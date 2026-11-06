/* Lyrics TV — экран. Ничего не считает сам: всё готовит бэкенд, здесь только отображение. */
(function () {
  'use strict';

  var qs = new URLSearchParams(location.search);
  var KEY = qs.get('key') || '';
  var EXTRA_OFFSET = parseInt(qs.get('offset') || '0', 10) || 0;   // ?offset=300 — подстроить на конкретном экране
  var ANCHOR = 0.3;                                                  // активная строка — на трети высоты карточки

  var $ = function (id) { return document.getElementById(id); };
  var body = document.body;
  var el = {
    bgA: $('bgA'), bgB: $('bgB'), cover: $('cover'), coverA: $('coverA'), coverB: $('coverB'),
    title: $('title'), titleText: $('titleText'), artist: $('artist'), fill: $('fill'), knob: $('knob'),
    elapsed: $('elapsed'), remaining: $('remaining'),
    lyrics: $('lyrics'), hint: $('hint'), viewport: $('viewport'), lines: $('lines'),
    time: $('time'), date: $('date'), clock: $('clock'),
    ntTitle: $('ntTitle'), ntBody: $('ntBody'), authchip: $('authchip')
  };

  var S = { status: 'connecting', track: null, lyrics: null, lyrics_state: 'none', offset: 0 };
  var basePos = 0, baseAt = performance.now(), playing = false;
  var rows = [], synced = false, activeIdx = -2, lastY = -1, lastSec = -1;
  var trackId = null, lyricsKey = null, paletteKey = null;
  var bgFront = el.bgA, coverFront = null;

  // ── позиция: сервер присылает раз в несколько секунд, между ними считаем сами ──
  function curPos() { return playing ? basePos + (performance.now() - baseAt) : basePos; }
  function setPos(p, isPlaying) {
    var predicted = curPos();
    // мелкие откаты назад — это дрожание отчёта Spotify, а не перемотка: игнорируем
    if (isPlaying && playing && p < predicted && predicted - p < 450) p = predicted;
    basePos = p; baseAt = performance.now(); playing = isPlaying;
  }
  function fmt(sec) { sec = Math.max(0, sec); return Math.floor(sec / 60) + ':' + ('0' + (sec % 60)).slice(-2); }

  // ── цвета ──
  function rgb(hex) { var n = parseInt(hex.slice(1), 16); return [n >> 16, (n >> 8) & 255, n & 255]; }
  function rgba(hex, a) { var c = rgb(hex); return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }
  function darker(hex, k) { var c = rgb(hex); return 'rgb(' + c.map(function (v) { return Math.round(v * (1 - k)); }).join(',') + ')'; }

  function applyPalette(p) {
    if (!p || !p.bg) return;
    var key = p.bg + p.card + p.glow;
    if (key === paletteKey) return;
    paletteKey = key;
    var back = bgFront === el.bgA ? el.bgB : el.bgA;
    back.style.background =
      'radial-gradient(130vh 110vh at 8% 6%, ' + rgba(p.glow, 0.55) + ' 0%, ' + rgba(p.glow, 0) + ' 62%),' +
      'linear-gradient(165deg, ' + p.bg + ' 0%, ' + darker(p.bg, 0.45) + ' 100%)';
    back.classList.add('on');
    bgFront.classList.remove('on');
    bgFront = back;
    el.lyrics.style.backgroundColor = p.card;
  }

  // ── обложка с плавной сменой ──
  function setCover(url) {
    var back = coverFront === el.coverA ? el.coverB : el.coverA;
    back.onload = function () {
      back.classList.add('on');
      if (coverFront && coverFront !== back) coverFront.classList.remove('on');
      coverFront = back;
      el.cover.classList.remove('empty');
    };
    back.onerror = function () {
      if (coverFront) coverFront.classList.remove('on');
      el.cover.classList.add('empty');
    };
    back.src = url;
  }

  // ── жирный CJK-шрифт трека: подмножество с сервера, пока грузится — системный шрифт ──
  var BASE_FONT = getComputedStyle(body).fontFamily, fontUrl = null, fontFace = null;
  var CJK_RANGE = 'U+3000-30FF, U+3400-4DBF, U+4E00-9FFF, U+F900-FAFF, U+FF00-FFEF, U+AC00-D7AF, ' +
                  'U+1100-11FF, U+3130-318F, U+31F0-31FF';
  function applyFont(url) {
    if (url === fontUrl) return;
    fontUrl = url;
    if (!url) { body.style.fontFamily = ''; return; }
    if (!window.FontFace || !document.fonts) return;
    var fam = 'CJK-' + url.replace(/[^A-Za-z0-9]/g, '').slice(-16);
    var face = new FontFace(fam, 'url(' + url + '&key=' + encodeURIComponent(KEY) + ')',
                            { weight: '100 900', unicodeRange: CJK_RANGE, display: 'swap' });
    face.load().then(function () {
      if (fontUrl !== url) return;                       // трек уже сменился
      document.fonts.add(face);
      if (fontFace) document.fonts.delete(fontFace);
      fontFace = face;
      body.style.fontFamily = '"' + fam + '", ' + BASE_FONT;
      activeIdx = -2; lastY = -1;                         // высоты строк изменились
      fitTitle();
    }).catch(function () { /* нет шрифта — остаётся системный */ });
  }

  // ── длинное название: до двух строк, иначе бегущая строка (только transform) ──
  var titleAnim = null, fitTimer = null;
  function fitTitle() {
    clearTimeout(fitTimer);
    fitTimer = setTimeout(function () {   // после раскладки и смены режима постера
      if (titleAnim) { titleAnim.cancel(); titleAnim = null; }
      el.title.classList.remove('marquee');
      if (el.title.scrollHeight <= el.title.clientHeight + 2) return;   // влезло в две строки
      el.title.classList.add('marquee');
      var dist = el.titleText.scrollWidth - el.title.clientWidth * 0.92;
      if (dist <= 0 || !el.titleText.animate) return;
      var move = Math.max(4000, dist / (window.innerHeight * 0.06) * 1000);   // ~6vh в секунду
      var pause = 3000, total = 2 * pause + 2 * move;
      titleAnim = el.titleText.animate([
        { transform: 'translate3d(0,0,0)', offset: 0 },
        { transform: 'translate3d(0,0,0)', offset: pause / total, easing: 'ease-in-out' },
        { transform: 'translate3d(' + (-dist) + 'px,0,0)', offset: (pause + move) / total },
        { transform: 'translate3d(' + (-dist) + 'px,0,0)', offset: (2 * pause + move) / total, easing: 'ease-in-out' },
        { transform: 'translate3d(0,0,0)', offset: 1 }
      ], { duration: total, iterations: Infinity });
    }, 60);
  }
  if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', fitTitle);

  // ── текст ──
  function setSub(d, text) {
    var r = d.querySelector('.r');
    if (!text) { if (r) d.removeChild(r); return; }
    if (!r) { r = document.createElement('div'); r.className = 'r'; d.appendChild(r); }
    if (r.textContent !== text) r.textContent = text;
  }

  function patchRomaji() {
    var L = S.lyrics, changed = false;
    if (!L || !L.lines) return;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i], ln = row.idx >= 0 ? L.lines[row.idx] : null;
      if (row.note || !ln) continue;
      var want = ln.translit || ln.romaji || ln.sub || '';
      if (want !== row.sub) { setSub(row.el, want); row.sub = want; changed = true; }
    }
    if (changed) { activeIdx = -2; lastY = -1; }   // высота строк изменилась — пересчитать прокрутку
  }

  function renderLyrics() {
    var L = S.lyrics;
    // ключ: трек, состояние, число строк, тайминги (поправка/масштаб); транслитерацию дописываем на месте
    var adj = L && L.adj ? L.adj.scale + ':' + L.adj.offset_ms : '';
    var key = (S.track ? S.track.id : '') + '|' + S.lyrics_state + '|' +
      (L && L.lines ? L.lines.length + ':' + L.synced + ':' + (L.source_id || '') + ':' + adj : 0);
    if (key === lyricsKey) { patchRomaji(); return; }   // тот же текст — ромадзи дописываем на месте
    lyricsKey = key;
    rows = []; activeIdx = -2; lastY = -1;
    el.lines.className = 'lines';
    el.lines.innerHTML = '';
    el.hint.textContent = '';

    if (S.lyrics_state === 'loading') {
      el.lines.classList.add('loading');
      el.lines.innerHTML = '<div class="sk"></div><div class="sk s2"></div><div class="sk s3"></div>';
      return;
    }
    if (!L || !L.lines || !L.lines.length) return;

    synced = !!L.synced;
    var src = L.lines.map(function (ln, i) { return { ln: ln, idx: i }; });
    if (synced && src[0].ln.t > 2500) src.unshift({ ln: { t: 0, text: '' }, idx: -1 });   // вступление — ♪
    var frag = document.createDocumentFragment();
    src.forEach(function (item) {
      var ln = item.ln, sub = ln.translit || ln.romaji || ln.sub || '';
      var d = document.createElement('div');
      if (!synced && !ln.text) { d.className = 'gap'; frag.appendChild(d); return; }
      d.className = 'line';
      var note = synced && !ln.text;
      if (note) {
        d.classList.add('note');
        d.textContent = '♪';
      } else {
        var o = document.createElement('div');
        o.className = 'o';
        o.textContent = ln.text;
        d.appendChild(o);
        setSub(d, sub);
      }
      frag.appendChild(d);
      rows.push({ t: ln.t || 0, el: d, note: note, idx: item.idx, sub: sub });
    });
    el.lines.appendChild(frag);
    if (!synced) { el.lines.classList.add('plain'); el.hint.textContent = 'без таймингов'; }
    el.lines.style.transition = 'none';
    el.lines.style.transform = 'translate3d(0,0,0)';
    void el.lines.offsetHeight;
    el.lines.style.transition = '';
  }

  function findIdx(pos) {
    var lo = 0, hi = rows.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (rows[mid].t <= pos) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }

  function scrollTo(y) {
    y = Math.max(0, Math.round(y));
    if (Math.abs(y - lastY) < 2) return;
    lastY = y;
    el.lines.style.transform = 'translate3d(0,' + (-y) + 'px,0)';
  }

  function updateLines(pos) {
    if (!rows.length) return;
    if (!synced) {   // текст без таймингов — медленно прокручиваем пропорционально треку
      var dur = S.track ? S.track.duration_ms : 0;
      if (!dur) return;
      var max = Math.max(0, el.lines.scrollHeight - el.viewport.clientHeight * 0.7);
      scrollTo(Math.min(1, pos / dur) * max);
      return;
    }
    var idx = findIdx(pos);
    if (idx === activeIdx) return;
    activeIdx = idx;
    for (var i = 0; i < rows.length; i++) {
      var cls = i < idx ? 'line past' : (i === idx ? 'line active' : 'line');
      if (rows[i].note) cls += ' note';
      if (rows[i].el.className !== cls) rows[i].el.className = cls;
    }
    scrollTo(idx >= 0 ? rows[idx].el.offsetTop - el.viewport.clientHeight * ANCHOR : 0);
  }

  // ── кадр ──
  function frame() {
    requestAnimationFrame(frame);
    var t = S.track;
    if (!t || S.status === 'idle') return;
    var pos = curPos();
    var dur = t.duration_ms || 0;
    var p = dur ? Math.min(pos, dur) : pos;
    var f = dur ? p / dur : 0;
    el.fill.style.transform = 'scaleX(' + f.toFixed(4) + ')';
    el.knob.style.left = (f * 100).toFixed(3) + '%';
    var sec = Math.floor(p / 1000);
    if (sec !== lastSec) {
      lastSec = sec;
      el.elapsed.textContent = fmt(sec);
      el.remaining.textContent = '−' + fmt(Math.ceil((dur - p) / 1000));
    }
    updateLines(pos + (S.offset || 0) + EXTRA_OFFSET);
  }

  // ── состояние с сервера ──
  function showNotice(st) {
    var host = location.hostname, port = location.port || '80';
    if (st === 'auth') {
      el.ntTitle.textContent = 'Нужен вход в Spotify';
      el.ntBody.innerHTML = 'На компьютере открой туннель: <code>ssh -L 8888:127.0.0.1:' + port + ' пользователь@' + host +
        '</code><br>и зайди в браузере на <code>http://127.0.0.1:8888/login</code>. Экран оживёт сам.';
    } else if (st === 'setup') {
      el.ntTitle.textContent = 'Сервер не настроен';
      el.ntBody.innerHTML = 'Заполни <code>SPOTIFY_CLIENT_ID</code> и <code>SPOTIFY_CLIENT_SECRET</code> в .env на ВМ и перезапусти сервис.';
    } else if (st === 'forbidden') {
      el.ntTitle.textContent = 'Нет доступа';
      el.ntBody.innerHTML = 'Проверь ключ в адресе экрана: <code>?key=…</code> должен совпадать с ACCESS_KEY на сервере.';
    }
  }

  function applyState(m) {
    S = m;
    setPos(m.pos || 0, !!m.playing);
    var st = m.status;
    body.classList.remove('boot');
    body.classList.toggle('connecting', st === 'connecting');
    body.classList.toggle('idle', st === 'idle');
    body.classList.toggle('paused', st === 'paused');
    body.classList.toggle('notice-on', st === 'auth' || st === 'setup');
    if (st === 'auth' || st === 'setup') showNotice(st);
    applyPalette(m.palette);

    var tr = m.track;
    if (tr && tr.id !== trackId) {
      trackId = tr.id;
      el.titleText.textContent = tr.title;
      fitTitle();
      el.artist.textContent = tr.artists;
      setCover(tr.cover + (tr.cover.indexOf('?') < 0 ? '?' : '&') + 'key=' + encodeURIComponent(KEY));
      lastSec = -1;
    }
    applyFont(tr && tr.font ? tr.font : null);
    var bare = !!tr && m.lyrics_state === 'none';
    if (bare !== body.classList.contains('bare')) { body.classList.toggle('bare', bare); fitTitle(); }
    renderLyrics();
    activeIdx = -2;

    var days = m.auth_days;
    if (days !== null && days !== undefined && days <= 10) {
      el.authchip.textContent = 'Вход в Spotify истечёт через ' + days + ' дн.';
      el.authchip.classList.add('on');
    } else {
      el.authchip.classList.remove('on');
    }
  }

  // ── связь с сервером ──
  var downSince = null, es = null;
  function connect() {
    es = new EventSource('/events?key=' + encodeURIComponent(KEY));
    es.onopen = function () { downSince = null; body.classList.remove('offline'); };
    es.onmessage = function (e) {
      downSince = null;
      body.classList.remove('offline');
      var m;
      try { m = JSON.parse(e.data); } catch (_) { return; }
      if (m.t === 'state') applyState(m);
      else if (m.t === 'pos') { setPos(m.pos, !!m.playing); body.classList.toggle('paused', !m.playing && S.status !== 'idle'); }
    };
    es.onerror = function () {
      if (downSince === null) downSince = Date.now();
      if (es.readyState === 2) {   // сервер ответил ошибкой (например, неверный ключ) — EventSource сдался
        body.classList.remove('boot');
        body.classList.add('notice-on');
        showNotice('forbidden');
        setTimeout(connect, 30000);
      }
    };
  }
  setInterval(function () {
    if (downSince && Date.now() - downSince > 6000) body.classList.add('offline');
  }, 1000);

  // ── часы ──
  var tf = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' });
  var df = new Intl.DateTimeFormat('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' });
  function tickClock() {
    var d = new Date();
    el.time.textContent = tf.format(d);
    var s = df.format(d);
    el.date.textContent = s.charAt(0).toUpperCase() + s.slice(1);
  }
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(function () {   // лёгкий сдвиг часов раз в минуту — бережём экран от выгорания
    el.clock.style.transform = 'translate(' + (Math.random() * 4 - 2).toFixed(2) + 'vw,' + (Math.random() * 4 - 2).toFixed(2) + 'vh)';
  }, 60000);

  window.addEventListener('resize', function () { activeIdx = -2; lastY = -1; fitTitle(); });

  connect();
  requestAnimationFrame(frame);
})();
