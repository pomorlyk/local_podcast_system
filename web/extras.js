/* Additive UI for two features that did not exist before:
     1. 订阅自动更新 —— 后台定时问每个订阅源有没有新单集，有新节目就在这里提醒。
     2. AI 发现 —— 按你写下的兴趣自己去找播客，并可以让模型替你决定关注哪几档。

   This file only ADDS things. It never edits, restyles or re-binds any element
   that the original interface already owns: the panel, the badge and the nav
   entries are created at runtime, and every new button uses an extras- prefixed
   data attribute so the original delegated click handler ignores it.
*/
(function () {
  'use strict';

  var POLL_MS = 60000;
  var state = { token: '', data: null, tab: 'updates', candidates: null, picked: {}, busy: false };

  // ---------------------------------------------------------------- helpers
  function el(id) { return document.getElementById(id); }

  function notify(message, isError) {
    try {
      if (typeof toast === 'function') { toast(message, !!isError); return; }
    } catch (error) { /* fall through to the local toast */ }
    var node = el('extras-local-toast');
    if (!node) {
      node = document.createElement('div');
      node.id = 'extras-local-toast';
      node.setAttribute('role', 'status');
      node.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);' +
        'background:#332e26;color:#faf4e9;padding:12px 20px;border-radius:4px;font-size:.83rem;z-index:40';
      document.body.appendChild(node);
    }
    node.textContent = message;
    node.style.background = isError ? '#954d38' : '#332e26';
    node.hidden = false;
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.hidden = true; }, isError ? 7000 : 3500);
  }

  function token() {
    if (state.token) return Promise.resolve(state.token);
    try {
      if (typeof app !== 'undefined' && app && app.token) { state.token = app.token; return Promise.resolve(state.token); }
    } catch (error) { /* not loaded yet */ }
    return fetch('/api/library', { cache: 'no-store' }).then(function (response) { return response.json(); })
      .then(function (body) { state.token = body.token || ''; return state.token; });
  }

  function call(path, payload, retried) {
    return token().then(function (value) {
      var options = payload
        ? { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Local-Token': value }, body: JSON.stringify(payload) }
        : { cache: 'no-store' };
      return fetch(path, options);
    }).then(function (response) {
      // A restarted local server issues a new page token; pick it up silently.
      if (response.status === 403 && !retried) {
        state.token = '';
        return call(path, payload, true);
      }
      return response.json().then(function (body) {
        if (!response.ok) throw new Error(body.error || '操作失败');
        return body;
      });
    });
  }

  // Reuse the original library loader so newly followed shows appear in the
  // existing sidebar, filter and list.
  function refreshLibrary() {
    try {
      if (typeof loadLibrary === 'function') { return Promise.resolve(loadLibrary()); }
    } catch (error) { /* fall back to a reload */ }
    location.reload();
    return Promise.resolve();
  }

  function escapeText(value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
  }

  function when(value) {
    if (!value) return '还没有检查过';
    var date = new Date(value);
    if (isNaN(date.getTime())) return String(value);
    var minutes = Math.round((Date.now() - date.getTime()) / 60000);
    if (minutes < 1) return '刚刚检查';
    if (minutes < 60) return minutes + ' 分钟前检查';
    if (minutes < 60 * 24) return Math.round(minutes / 60) + ' 小时前检查';
    return Math.round(minutes / 1440) + ' 天前检查';
  }

  function coverHTML(cover, label) {
    return '<span class="extras-cover">' +
      (cover ? '<img src="' + escapeText(cover) + '" alt="" loading="lazy">' : escapeText((label || '?').slice(0, 2).toUpperCase())) +
      '</span>';
  }

  // ------------------------------------------------------------- shell/DOM
  function buildShell() {
    if (el('extras-panel')) return;

    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = '/extras.css';
    document.head.appendChild(link);

    var backdrop = document.createElement('div');
    backdrop.id = 'extras-backdrop';
    backdrop.hidden = true;
    backdrop.addEventListener('click', close);

    var panel = document.createElement('section');
    panel.id = 'extras-panel';
    panel.hidden = true;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-label', '订阅更新与发现');
    panel.innerHTML =
      '<header class="extras-head">' +
        '<div class="extras-tabs" role="tablist">' +
          '<button type="button" role="tab" data-extras-tab="updates" aria-selected="true">新节目</button>' +
          '<button type="button" role="tab" data-extras-tab="discover" aria-selected="false">AI 发现</button>' +
          '<button type="button" role="tab" data-extras-tab="settings" aria-selected="false">设置</button>' +
        '</div>' +
        '<button type="button" class="extras-close" data-extras-close aria-label="关闭">×</button>' +
      '</header>' +
      '<div class="extras-body">' +
        '<div data-extras-pane="updates">' +
          '<p class="extras-note">开启后，本机会按设定间隔自动去问每个订阅源有没有新单集；只更新目录，不会自动下载音频。</p>' +
          '<div class="extras-row">' +
            '<label class="extras-check"><input type="checkbox" id="extras-auto-enabled"> 定时自动检查</label>' +
            '<span class="extras-field extras-inline">每 <input type="number" id="extras-interval" min="5" max="1440" step="5"> 分钟</span>' +
            '<button type="button" class="button" data-extras-refresh>立即检查全部</button>' +
            '<button type="button" class="text-button" data-extras-seen-all>全部标记已读</button>' +
            '<button type="button" class="text-button" data-extras-save-auto>保存设置</button>' +
          '</div>' +
          '<p class="extras-status" id="extras-updates-status"></p>' +
          '<div id="extras-updates-list"></div>' +
        '</div>' +
        '<div data-extras-pane="discover" hidden>' +
          '<p class="extras-note">系统用公开的 Apple Podcasts 检索接口找候选，再让配置好的模型按你的兴趣挑出真正想听的，最后自动关注。没有配置模型时，会用本地规则排序，同样能关注。</p>' +
          '<div class="extras-field wide"><label for="extras-keywords">你感兴趣的主题（逗号或换行分隔）</label>' +
            '<textarea id="extras-keywords" placeholder="例如：AI 编程、自动驾驶、独立游戏开发、产品设计"></textarea></div>' +
          '<div class="extras-row">' +
            '<span class="extras-field"><label for="extras-country">检索地区</label>' +
              '<select id="extras-country"><option value="US">美国 US</option><option value="GB">英国 GB</option>' +
              '<option value="CN">中国 CN</option><option value="JP">日本 JP</option><option value="DE">德国 DE</option>' +
              '<option value="AU">澳大利亚 AU</option><option value="CA">加拿大 CA</option></select></span>' +
            '<span class="extras-field"><label for="extras-limit">每次自动关注几档</label>' +
              '<input type="number" id="extras-limit" min="1" max="10" step="1" style="width:88px"></span>' +
            '<button type="button" class="text-button" data-extras-save-interests>保存兴趣</button>' +
          '</div>' +
          '<div class="extras-row">' +
            '<button type="button" class="button primary" data-extras-auto>让 AI 自己去找并关注</button>' +
            '<button type="button" class="button" data-extras-recommend>只推荐给我看</button>' +
          '</div>' +
          '<p class="extras-status" id="extras-discover-status"></p>' +
          '<div class="extras-row" style="margin-top:22px">' +
            '<span class="extras-field wide"><label for="extras-query">或直接搜索播客名字</label>' +
              '<input type="text" id="extras-query" placeholder="输入节目名、主持人和关键词"></span>' +
            '<button type="button" class="text-button" data-extras-search style="align-self:flex-end">搜索</button>' +
          '</div>' +
          '<div id="extras-results"></div>' +
        '</div>' +
        '<div data-extras-pane="settings" hidden>' +
          '<p class="extras-note">模型密钥只保存在这台电脑上，仓库里不会包含任何密钥。也可以改用环境变量 <code>PODCAST_LLM_API_KEY</code>、<code>PODCAST_LLM_BASE_URL</code>、<code>PODCAST_LLM_MODEL</code>。</p>' +
          '<p class="extras-status" id="extras-key-status"></p>' +
          '<div class="extras-row">' +
            '<span class="extras-field"><label for="extras-provider">服务商</label>' +
              '<select id="extras-provider"></select></span>' +
            '<span class="extras-field wide"><label for="extras-base">接口地址</label>' +
              '<input type="text" id="extras-base" placeholder="https://api.deepseek.com"></span>' +
            '<span class="extras-field"><label for="extras-model">模型</label>' +
              '<input type="text" id="extras-model" placeholder="deepseek-chat"></span>' +
          '</div>' +
          '<div class="extras-field wide"><label for="extras-key">API 密钥（留空表示沿用已保存的）</label>' +
            '<input type="password" id="extras-key" autocomplete="new-password" placeholder="粘贴你自己的密钥"></div>' +
          '<div class="extras-row" style="margin-top:16px">' +
            '<button type="button" class="button primary" data-extras-save-key>保存设置</button>' +
            '<button type="button" class="text-button" data-extras-remove-key>清除已保存的密钥</button>' +
          '</div>' +
          '<p class="extras-note" style="margin-top:26px">已关注的节目</p>' +
          '<div id="extras-followed"></div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(backdrop);
    document.body.appendChild(panel);
    panel.addEventListener('click', onClick);
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !panel.hidden) close();
    });
  }

  function buildEntryPoints() {
    var nav = document.querySelector('.sidebar nav[aria-label="资料库导航"]');
    if (nav && !el('extras-nav')) {
      var button = document.createElement('button');
      button.className = 'nav-item';
      button.id = 'extras-nav';
      button.type = 'button';
      button.innerHTML = '更新与发现 <b id="extras-badge">0</b>';
      button.addEventListener('click', function () { open('updates'); });
      nav.appendChild(button);
    }
    var actions = document.querySelector('.topbar-actions');
    if (actions && !el('extras-bell')) {
      var bell = document.createElement('button');
      bell.className = 'text-button';
      bell.id = 'extras-bell';
      bell.type = 'button';
      bell.title = '新节目';
      bell.textContent = '新节目';
      bell.addEventListener('click', function () { open('updates'); });
      actions.insertBefore(bell, actions.firstChild);
    }
  }

  // ------------------------------------------------------------- panel ops
  function open(tab) {
    buildShell();
    state.tab = tab || 'updates';
    el('extras-backdrop').hidden = false;
    el('extras-panel').hidden = false;
    selectTab(state.tab);
    loadUpdates().then(renderUpdates);
    if (state.tab === 'discover') loadDiscover();
    if (state.tab === 'settings') loadSettings();
  }

  function close() {
    if (el('extras-panel')) el('extras-panel').hidden = true;
    if (el('extras-backdrop')) el('extras-backdrop').hidden = true;
  }

  function selectTab(tab) {
    state.tab = tab;
    Array.prototype.forEach.call(document.querySelectorAll('[data-extras-tab]'), function (button) {
      button.setAttribute('aria-selected', String(button.getAttribute('data-extras-tab') === tab));
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-extras-pane]'), function (pane) {
      pane.hidden = pane.getAttribute('data-extras-pane') !== tab;
    });
  }

  function status(id, message, isError) {
    var node = el(id);
    node.textContent = message || '';
    node.classList.toggle('error', !!isError);
  }

  // --------------------------------------------------------------- updates
  function loadUpdates() {
    return call('/api/updates').then(function (data) {
      state.data = data;
      var total = data.unseen_total || 0;
      var badge = el('extras-badge');
      if (badge) badge.textContent = String(total);
      if (el('extras-nav')) el('extras-nav').setAttribute('data-hot', total ? '1' : '0');
      if (el('extras-bell')) {
        el('extras-bell').setAttribute('data-hot', total ? '1' : '0');
        el('extras-bell').textContent = total ? '新节目 ' + total : '新节目';
      }
      return data;
    }).catch(function (error) { status('extras-updates-status', error.message, true); });
  }

  function renderUpdates() {
    var data = state.data;
    if (!data) return;
    var settings = data.settings || {};
    if (el('extras-auto-enabled')) el('extras-auto-enabled').checked = !!settings.enabled;
    if (el('extras-interval')) el('extras-interval').value = settings.interval_minutes || 30;
    var running = data.status && data.status.running;
    status('extras-updates-status', running
      ? '正在检查：' + (data.status.current || '') + '（' + (data.status.done || 0) + '/' + (data.status.total || 0) + '）'
      : '共 ' + (data.unseen_total || 0) + ' 条新单集未读；' + (settings.enabled
        ? '每 ' + (settings.interval_minutes || 30) + ' 分钟自动检查一次，' + when((data.settings || {}).last_run * 1000)
        : '当前只在你手动点击时检查'));

    var rows = (data.shows || []).filter(function (show) { return show.new_episodes && show.new_episodes.length; });
    var failed = (data.shows || []).filter(function (show) { return show.last_ok === false; });
    var html = '';
    if (!rows.length) {
      html = '<div class="extras-empty">暂时没有新单集。<br>开启「定时自动检查」后，有新节目这里会出现提醒。</div>';
    } else {
      rows.forEach(function (show) {
        html += '<div class="extras-card"><div class="extras-card-head">' + coverHTML(show.cover, show.name) +
          '<strong>' + escapeText(show.name) + '</strong>' +
          '<span class="extras-meta">' + escapeText(show.category || '') + ' · ' + escapeText(when(show.last_checked)) + '</span>' +
          '<span class="extras-push"><span class="extras-tag on">' + show.new_episodes.length + ' 条新</span>' +
          '<button type="button" class="text-button" data-extras-seen-show="' + escapeText(show.id) + '">标记已读</button></span>' +
          '</div><ul class="extras-new-list">' +
          show.new_episodes.map(function (row) {
            return '<li><span class="extras-date">' + escapeText(row.date || '') + '</span>' +
              '<span class="extras-title">' + escapeText(row.title || '（未命名单集）') + '</span>' +
              '<span class="extras-actions">' +
              '<button type="button" class="text-button" data-extras-open="' + escapeText(row.key) + '">查看</button>' +
              '<button type="button" class="text-button" data-extras-download="' + escapeText(row.key) + '">下载</button>' +
              '</span></li>';
          }).join('') + '</ul></div>';
      });
    }
    if (failed.length) {
      html += '<div class="extras-card"><div class="extras-card-head"><strong>暂时连不上的订阅源</strong></div>' +
        '<ul class="extras-new-list">' + failed.map(function (show) {
          return '<li><span class="extras-title">' + escapeText(show.name) + '：' + escapeText(show.error || '未知错误') + '</span></li>';
        }).join('') + '</ul></div>';
    }
    el('extras-updates-list').innerHTML = html;
  }

  // -------------------------------------------------------------- discover
  function loadDiscover() {
    return call('/api/discover/interests').then(function (profile) {
      el('extras-keywords').value = (profile.keywords || []).join('、');
      el('extras-country').value = profile.country || 'US';
      el('extras-limit').value = profile.auto_subscribe_limit || 3;
      return profile;
    }).catch(function (error) { status('extras-discover-status', error.message, true); });
  }

  function renderCandidates(payload) {
    state.candidates = payload.candidates || [];
    state.picked = {};
    var notes = [];
    notes.push(payload.ranking === 'ai' ? '由模型挑选' : '由本地规则排序');
    if (payload.ai_error) notes.push('模型未参与：' + payload.ai_error);
    if (payload.errors && payload.errors.length) notes.push('部分关键词检索失败');
    var html = '<p class="extras-note">' + escapeText(notes.join(' · ')) + '</p>';
    if (!state.candidates.length) {
      html += '<div class="extras-empty">没有找到候选节目，换个关键词或地区再试。</div>';
    } else {
      html += state.candidates.map(function (row) {
        var id = String(row.apple_id);
        var reason = (payload.reasons || {})[id];
        return '<div class="extras-candidate">' + coverHTML(null, row.name) +
          '<div class="extras-candidate-body"><strong>' + escapeText(row.name) + '</strong>' +
          '<p class="extras-meta"><span>' + escapeText(row.author || '') + '</span>' +
          '<span>' + escapeText((row.genres || []).slice(0, 2).join(' / ')) + '</span>' +
          '<span>' + (row.episodes || 0) + ' 期</span>' +
          (row.already_followed ? '<span class="extras-tag">已关注</span>' : '') + '</p>' +
          (reason ? '<p class="extras-reason">' + escapeText(reason) + '</p>' : '') +
          '</div><div class="extras-candidate-actions">' +
          '<span class="extras-score">匹配 ' + (row.score || 0) + '</span>' +
          (row.already_followed ? '' : '<label class="extras-check"><input type="checkbox" data-extras-pick="' + escapeText(id) + '"> 选择</label>') +
          '</div></div>';
      }).join('');
      html += '<div class="extras-row" style="margin-top:18px">' +
        '<button type="button" class="button primary" data-extras-subscribe-picked>关注选中的节目</button></div>';
    }
    el('extras-results').innerHTML = html;
  }

  function renderSearchResults(results) {
    state.candidates = results;
    if (!results.length) {
      el('extras-results').innerHTML = '<div class="extras-empty">没有搜到节目。</div>';
      return;
    }
    el('extras-results').innerHTML = '<p class="extras-note">搜索结果 · ' + results.length + ' 档</p>' +
      results.map(function (row) {
        return '<div class="extras-candidate">' + coverHTML(null, row.name) +
          '<div class="extras-candidate-body"><strong>' + escapeText(row.name) + '</strong>' +
          '<p class="extras-meta"><span>' + escapeText(row.author || '') + '</span>' +
          '<span>' + escapeText((row.genres || []).slice(0, 2).join(' / ')) + '</span>' +
          '<span>' + (row.episodes || 0) + ' 期</span></p></div>' +
          '<div class="extras-candidate-actions">' +
          '<button type="button" class="text-button" data-extras-follow="' + escapeText(String(row.apple_id)) + '">关注</button>' +
          '</div></div>';
      }).join('') + '<div class="extras-row" style="margin-top:18px">' +
      '<button type="button" class="button primary" data-extras-subscribe-picked>关注已勾选的节目</button></div>';
  }

  // Use whatever is in the boxes right now, so a query never silently runs
  // against a stale saved profile.
  function interestsPayload() {
    return {
      keywords: el('extras-keywords').value,
      country: el('extras-country').value,
      limit: Number(el('extras-limit').value) || 3
    };
  }

  function payloadFor(id) {    var row = (state.candidates || []).filter(function (item) { return String(item.apple_id) === String(id); })[0];
    if (!row) return null;
    return {
      name: row.name, feed_url: row.feed_url, apple_id: row.apple_id, apple_url: row.apple_url,
      artwork_url: row.artwork_url, genres: row.genres, reason: (state.reasons || {})[String(id)] || '',
      picked_by: 'manual'
    };
  }

  function subscribe(rows) {
    if (!rows.length) { notify('先勾选要关注的节目', true); return Promise.resolve(); }
    status('extras-discover-status', '正在关注 ' + rows.length + ' 档节目并同步目录…');
    return call('/api/discover/subscribe', { shows: rows }).then(function (result) {
      var ok = (result.results || []).filter(function (row) { return row.status === 'followed'; });
      var followFailed = (result.results || []).filter(function (row) { return row.status === 'failed'; });
      status('extras-discover-status', '已关注 ' + ok.length + ' 档' +
        (followFailed.length ? '，' + followFailed.length + ' 档失败：' + followFailed.map(function (r) { return r.name; }).join('、') : ''),
        !!followFailed.length);
      return refreshLibrary();
    }).catch(function (error) { status('extras-discover-status', error.message, true); });
  }

  function renderFollowed(rows) {
    el('extras-followed').innerHTML = rows.length ? rows.map(function (row) {
      return '<div class="extras-followed">' + coverHTML(row.cover, row.name) +
        '<span>' + escapeText(row.name) + '</span>' +
        '<span class="extras-meta">' + escapeText(row.category || '') + ' · ' + (row.count || 0) + ' 期</span>' +
        '<button type="button" class="text-button" data-extras-unfollow="' + escapeText(row.id) + '">取消关注</button></div>';
    }).join('') : '<div class="extras-empty">还没有关注任何节目。</div>';
  }

  function loadSettings() {
    return Promise.all([call('/api/discover/settings'), call('/api/library')]).then(function (pair) {
      var settings = pair[0];
      var options = Object.keys(settings.providers || {}).map(function (key) {
        var item = settings.providers[key];
        return '<option value="' + escapeText(key) + '">' + escapeText(item.label) + '</option>';
      }).join('');
      el('extras-provider').innerHTML = options;
      el('extras-provider').value = settings.provider;
      el('extras-base').value = settings.base_url || '';
      el('extras-model').value = settings.model || '';
      el('extras-key').value = '';
      status('extras-key-status', settings.configured
        ? '已配置密钥（来源：' + (settings.key_source === 'environment' ? '环境变量' : '本机加密文件') + '）。留空保存即可沿用。'
        : '还没有配置密钥。没有密钥也能用「本地规则排序」去发现节目。');
      renderFollowed(pair[1].shows || []);
    }).catch(function (error) { status('extras-key-status', error.message, true); });
  }

  // ------------------------------------------------------------- behaviour
  function onClick(event) {
    var target = event.target.closest('button');
    var attribute = function (name) {
      var node = event.target.closest('[' + name + ']');
      return node ? node.getAttribute(name) : null;
    };
    var tab = attribute('data-extras-tab');
    if (tab) { selectTab(tab); if (tab === 'discover') loadDiscover(); if (tab === 'settings') loadSettings(); return; }
    if (attribute('data-extras-close')) { close(); return; }

    var refresh = target && target.hasAttribute('data-extras-refresh');
    if (refresh) {
      status('extras-updates-status', '已开始检查全部订阅源…');
      call('/api/updates/refresh', { trigger: 'manual' })
        .then(function () { setTimeout(function () { loadUpdates().then(renderUpdates); }, 1200); })
        .catch(function (error) { status('extras-updates-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-save-auto')) {
      call('/api/subscription-settings', {
        enabled: el('extras-auto-enabled').checked,
        interval_minutes: Number(el('extras-interval').value) || 30
      }).then(function () { notify('定时检查设置已保存'); return loadUpdates().then(renderUpdates); })
        .catch(function (error) { status('extras-updates-status', error.message, true); });
      return;
    }
    var seenShow = attribute('data-extras-seen-show');
    if (seenShow) {
      call('/api/updates/seen', { show: seenShow }).then(function (data) { state.data = data; renderUpdates(); })
        .catch(function (error) { status('extras-updates-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-seen-all')) {
      call('/api/updates/seen', { all: true }).then(function (data) {
        state.data = data; renderUpdates(); loadUpdates();
      }).catch(function (error) { status('extras-updates-status', error.message, true); });
      return;
    }
    var openKey = attribute('data-extras-open');
    if (openKey) {
      close();
      try {
        if (typeof selectEpisode === 'function') { selectEpisode(openKey).catch(function (error) { notify(error.message, true); }); }
        else location.hash = 'listen=' + encodeURIComponent(openKey);
      } catch (error) { location.hash = 'listen=' + encodeURIComponent(openKey); }
      return;
    }
    var downloadKey = attribute('data-extras-download');
    if (downloadKey) {
      var parts = downloadKey.split(':');
      call('/api/job', { kind: 'download', show: parts[0], episode: parts[1] })
        .then(function () { notify('已加入下载队列'); return refreshLibrary(); })
        .catch(function (error) { notify(error.message, true); });
      return;
    }

    if (target && target.hasAttribute('data-extras-save-interests')) {
      call('/api/discover/interests', {
        keywords: el('extras-keywords').value,
        country: el('extras-country').value,
        auto_subscribe_limit: Number(el('extras-limit').value) || 3
      }).then(function () { notify('兴趣已保存'); }).catch(function (error) { status('extras-discover-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-auto')) {
      status('extras-discover-status', '正在检索并让模型挑选，请稍等…');
      call('/api/discover/auto', interestsPayload())
        .then(function (report) {
          renderCandidates(report);
          var followed = (report.subscribed || []).filter(function (row) { return row.status === 'followed'; });
          status('extras-discover-status', followed.length
            ? '已自动关注 ' + followed.map(function (row) { return row.name; }).join('、')
            : (report.message || '没有新增关注'));
          if (followed.length) return refreshLibrary();
        })
        .catch(function (error) { status('extras-discover-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-recommend')) {
      status('extras-discover-status', '正在检索候选节目，请稍等…');
      call('/api/discover/recommend', interestsPayload())
        .then(function (report) { renderCandidates(report); status('extras-discover-status', '共 ' + (report.candidates || []).length + ' 个候选，勾选后可以一起关注。'); })
        .catch(function (error) { status('extras-discover-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-search')) {
      var query = el('extras-query').value.trim();
      if (!query) { notify('先输入要搜索的名字', true); return; }
      status('extras-discover-status', '正在搜索…');
      call('/api/discover/search?q=' + encodeURIComponent(query) + '&country=' + encodeURIComponent(el('extras-country').value))
        .then(function (data) { renderSearchResults(data.results || []); status('extras-discover-status', '搜索完成'); })
        .catch(function (error) { status('extras-discover-status', error.message, true); });
      return;
    }
    var followId = attribute('data-extras-follow');
    if (followId) {
      var row = payloadFor(followId);
      if (row) subscribe([row]);
      return;
    }
    if (target && target.hasAttribute('data-extras-subscribe-picked')) {
      var picked = Array.prototype.slice.call(document.querySelectorAll('[data-extras-pick]'))
        .filter(function (box) { return box.checked; })
        .map(function (box) { return payloadFor(box.getAttribute('data-extras-pick')); })
        .filter(Boolean);
      subscribe(picked);
      return;
    }

    if (target && target.hasAttribute('data-extras-save-key')) {
      call('/api/discover/settings', {
        provider: el('extras-provider').value,
        base_url: el('extras-base').value.trim(),
        model: el('extras-model').value.trim(),
        api_key: el('extras-key').value.trim()
      }).then(function () { notify('模型设置已保存'); return loadSettings(); })
        .catch(function (error) { status('extras-key-status', error.message, true); });
      return;
    }
    if (target && target.hasAttribute('data-extras-remove-key')) {
      call('/api/discover/settings', { provider: el('extras-provider').value, remove_key: true })
        .then(function () { notify('已清除本机保存的密钥'); return loadSettings(); })
        .catch(function (error) { status('extras-key-status', error.message, true); });
      return;
    }
    var unfollow = attribute('data-extras-unfollow');
    if (unfollow) {
      call('/api/discover/unfollow', { show: unfollow })
        .then(function () { notify('已取消关注'); return refreshLibrary(); })
        .then(loadSettings)
        .catch(function (error) { status('extras-key-status', error.message, true); });
    }
  }

  function onChange(event) {
    var provider = event.target;
    if (provider.id !== 'extras-provider') return;
    call('/api/discover/settings').then(function (settings) {
      var item = (settings.providers || {})[provider.value];
      if (!item) return;
      if (item.base_url) el('extras-base').value = item.base_url;
      if (item.models && item.models.length) el('extras-model').value = item.models[0];
    }).catch(function () { /* keep whatever the user typed */ });
  }

  // ----------------------------------------------------------------- start
  function start() {
    buildShell();
    buildEntryPoints();
    loadUpdates().then(renderUpdates);
    setInterval(function () {
      loadUpdates().then(function () { if (state.tab === 'updates' && !el('extras-panel').hidden) renderUpdates(); });
    }, POLL_MS);
    document.addEventListener('change', onChange);
    // ?panel=updates|discover|settings opens the panel straight away, so the
    // view can be bookmarked or shared without touching the original hash
    // routing that app.js uses for #listen=<episode>.
    try {
      var wanted = new URLSearchParams(location.search).get('panel');
      if (wanted && ['updates', 'discover', 'settings'].indexOf(wanted) >= 0) open(wanted);
    } catch (error) { /* older engines: just show the shelf */ }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
