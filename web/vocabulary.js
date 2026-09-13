'use strict';
app.vocabulary=[];app.vocabularyAll=[];app.wordTrash=false;app.captureEnabled=true;app.selecting=false;app.followAfter=0;
let vocabularyPolling=false,lastVocabularyJSON='',captureSequence=0,suppressSubtitleClickUntil=0;

function hasSubtitleSelection(){const s=window.getSelection();return !!(s&&!s.isCollapsed&&s.anchorNode&&$('#transcript-content').contains(s.anchorNode));}
function highlightVocabulary(text,index,language){
  const matches=(app.vocabulary||[]).filter(w=>w.transcript_id===app.selected?.transcript?.id&&w.cue_index===index&&w.language===language);
  const intervals=[];
  for(const w of matches){let start=0;while((start=text.indexOf(w.selected,start))>=0){intervals.push([start,start+w.selected.length]);start+=w.selected.length;}}
  intervals.sort((a,b)=>a[0]-b[0]);const merged=[];
  for(const r of intervals){const prev=merged.at(-1);if(prev&&r[0]<=prev[1])prev[1]=Math.max(prev[1],r[1]);else merged.push([...r]);}
  let pos=0,html='';for(const [start,end] of merged){html+=escapeHTML(text.slice(pos,start))+'<mark class="saved-word">'+escapeHTML(text.slice(start,end))+'</mark>';pos=end;}
  return html+escapeHTML(text.slice(pos));
}
function vocabularyCard(w){
  const ep=getEpisode(w.episode),pending=['pending','running'].includes(w.status);
  return `<article class="word-card"><div class="word-heading"><strong>${escapeHTML(w.english||w.selected)}</strong>${w.deleted?`<button class="text-button" data-word-restore="${w.id}">恢复收藏</button>`:`<button class="text-button" data-word-delete="${w.id}" aria-label="移除 ${escapeHTML(w.selected)}">移除</button>`}</div><p class="word-meaning">${escapeHTML(w.chinese||(pending?'正在补齐中文词义…':'词义尚未补齐'))}</p><div class="word-source"><span>${escapeHTML(showName(w.episode.split(':')[0]))}</span>${w.start!==null?`<button class="text-button" data-word-play="${w.id}">回听 ${clock(w.start)}</button>`:'<span>无时间轴</span>'}<time>${new Date(w.created*1000).toLocaleDateString('zh-CN')}</time></div><details><summary>中英原句 · ${escapeHTML(ep?.title||w.episode)}</summary><p lang="en">${escapeHTML(w.context_en)}</p><p lang="zh">${escapeHTML(w.context_zh||'保存时中文稿尚未生成')}</p></details>${pending?'<small class="muted">原词已保存，正在补齐双语词义</small>':['failed','waiting_key'].includes(w.status)?`<div class="word-error">${escapeHTML(w.error)} <button class="text-button" data-word-retry="${w.id}">补齐词义</button></div>`:''}</article>`;
}
function renderVocabulary(){
  $('#vocabulary-count').textContent=app.vocabulary.length;
  const words=app.vocabulary.filter(w=>w.episode===app.selected?.key);
  $('#episode-vocabulary').innerHTML=words.length?words.map(vocabularyCard).join(''):'<p class="mark-empty">听的时候划选一个词，这里会留下它的双语释义和原句。</p>';
}
function renderVocabularyLibrary(){
  $('#search').placeholder='找一个词或一句话';document.querySelector('.list-heading>span:last-child').textContent='双语 / 原句';
  $('#breadcrumb').textContent='生词本';$('#page-title').textContent='听过，也记住。';$('#page-caption').textContent='划选留下的词、双语原句，和那一刻的声音。';
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view==='vocabulary'));
  const query=$('#search').value.trim().toLowerCase();
  const words=(app.wordTrash?app.vocabularyAll.filter(w=>w.deleted):app.vocabulary).filter(w=>(!app.show||w.episode.startsWith(app.show+':'))&&(!query||[w.selected,w.english,w.chinese,w.context_en,w.context_zh,getEpisode(w.episode)?.title].join(' ').toLowerCase().includes(query)));
  $('#list-count').textContent=words.length+' 条收藏';$('#more').hidden=words.length<=app.limit;
  $('#episodes').innerHTML=`<button class="text-button" id="word-trash-toggle">${app.wordTrash?'← 返回生词本':'查看已移除的收藏'}</button>`+'<a class="word-export" href="/api/vocabulary-export" download>导出生词本 JSON ↗</a>'+(words.slice(0,app.limit).map(vocabularyCard).join('')||'<p class="empty">'+(query?'没有找到这个词。':'播放时划选字幕，生词会自动保存在这里。')+'</p>');
}
async function refreshVocabulary(){
  if(vocabularyPolling)return;vocabularyPolling=true;
  try{const result=await api('/api/vocabulary?include_deleted=1'),serialized=JSON.stringify(result.items);
    app.vocabularyAll=result.items;app.vocabulary=result.items.filter(w=>!w.deleted);renderVocabulary();if(app.view==='vocabulary')renderVocabularyLibrary();
    if(serialized!==lastVocabularyJSON&&!app.selecting&&!hasSubtitleSelection()&&app.selected?.transcript){renderTranscript();lastVocabularyJSON=serialized;}
  }finally{vocabularyPolling=false;}
}
function capturePayload(){
  const selection=window.getSelection();if(!selection||selection.isCollapsed)return null;
  const element=node=>node?.nodeType===Node.ELEMENT_NODE?node:node?.parentElement;
  const a=element(selection.anchorNode)?.closest('.subtitle-en,.subtitle-zh'),b=element(selection.focusNode)?.closest('.subtitle-en,.subtitle-zh');
  if(!a||a!==b||!$('#transcript-content').contains(a))return null;
  let selected=selection.toString().trim();const row=a.closest('[data-cue-index]');
  if(!selected)return null;
  if(a.classList.contains('subtitle-en')){
    // A quick sweep may stop between letters. Keep whole English words/compounds.
    const range=selection.getRangeAt(0),prefix=document.createRange();prefix.selectNodeContents(a);prefix.setEnd(range.startContainer,range.startOffset);
    let start=prefix.toString().length,end=start+range.toString().length;const text=a.textContent,word=c=>!!c&&/[\p{L}\p{N}'’\-]/u.test(c);
    while(start>0&&word(text[start-1]))start--;
    while(end<text.length&&word(text[end]))end++;
    selected=text.slice(start,end).trim();
  }
  if(!selected||!row||a.classList.contains('subtitle-pending'))return null;
  return {...basePayload(app.selected),transcript_id:app.selected.transcript.id,cue_index:Number(row.dataset.cueIndex),selected,language:a.classList.contains('subtitle-en')?'en':'zh'};
}
async function saveSelection(payload){
  const seq=++captureSequence;app.lastCapture=payload;
  $('#capture-status').textContent='正在保存「'+payload.selected+'」…';
  try{const result=await api('/api/vocabulary',payload);
    if(seq===captureSequence){app.lastCapture=null;$('#capture-status').innerHTML=`${result.duplicate?'已收藏过':'已收藏'}「${escapeHTML(payload.selected)}」· 双语原句已留档 <button class="text-button" data-word-delete="${result.id}">撤销</button>`;}
    await refreshVocabulary();
  }catch(e){if(seq===captureSequence)$('#capture-status').innerHTML=`保存失败：${escapeHTML(e.message)} <button id="retry-capture" class="text-button">重试保存</button>`;}
}
function finishSelection(){
  app.selecting=false;if(!hasSubtitleSelection())return;
  suppressSubtitleClickUntil=Date.now()+500;app.followAfter=Date.now()+2500;
  if(!app.captureEnabled)return;
  const payload=capturePayload();if(!payload){$('#capture-status').textContent='请在同一句英文或中文里划选一个词或短语。';return;}
  window.getSelection().removeAllRanges();void saveSelection(payload);
}
$('#capture-toggle').onclick=()=>{app.captureEnabled=!app.captureEnabled;$('#capture-toggle').setAttribute('aria-pressed',String(app.captureEnabled));$('#capture-toggle').textContent='划选即收藏 · '+(app.captureEnabled?'开':'关');};
$('#transcript-content').addEventListener('pointerdown',()=>{app.selecting=true;});
document.addEventListener('pointerup',()=>{if(app.selecting)setTimeout(finishSelection,0);});
document.addEventListener('pointercancel',()=>{app.selecting=false;app.followAfter=Date.now()+1500;});
document.addEventListener('keyup',event=>{if(event.key==='Shift'&&hasSubtitleSelection())finishSelection();});
$('#transcript-content').addEventListener('click',event=>{
  if(hasSubtitleSelection()||Date.now()<suppressSubtitleClickUntil){event.preventDefault();event.stopPropagation();return;}
  const row=event.target.closest('.transcript-line[data-start]');
  if(row&&!event.target.closest('button')){event.stopPropagation();void guard(jump)(Number(row.dataset.start));}
},true);
document.addEventListener('click',guard(async event=>{
  const t=event.target.closest('button');if(!t)return;
  if(t.id==='word-trash-toggle'){app.wordTrash=!app.wordTrash;renderVocabularyLibrary();}
  if(t.id==='retry-capture'&&app.lastCapture)await saveSelection(app.lastCapture);
  if(t.dataset.wordDelete){await api('/api/vocabulary-delete',{id:t.dataset.wordDelete});$('#capture-status').innerHTML=`已移出生词本 <button class="text-button" data-word-restore="${t.dataset.wordDelete}">恢复</button>`;await refreshVocabulary();}
  if(t.dataset.wordRestore){await api('/api/vocabulary-restore',{id:t.dataset.wordRestore});$('#capture-status').textContent='已恢复收藏';await refreshVocabulary();}
  if(t.dataset.wordRetry){await api('/api/vocabulary-retry',{id:t.dataset.wordRetry});await refreshVocabulary();}
  if(t.dataset.wordPlay){const word=app.vocabulary.find(w=>w.id===t.dataset.wordPlay);if(!word)return;await selectEpisode(word.episode);if(app.selected.asset!==word.asset)throw Error('音频版本已变化，保留了原句，但无法在当前版本准确回听');app.follow=true;app.followAfter=0;$('#follow-subtitles').setAttribute('aria-pressed','true');$('#follow-subtitles').textContent='自动滚动';await jump(word.start);}
}));
setInterval(()=>{if(app.vocabulary.some(w=>['pending','running','waiting_key'].includes(w.status)))void refreshVocabulary().catch(()=>{});},3000);
