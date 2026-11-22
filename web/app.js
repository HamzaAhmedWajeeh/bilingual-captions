/* Audience page, and the caption feed both pages share.
 *
 * `createFeed` is the whole client side of the broadcast protocol:
 *
 *   server -> {"type":"snapshot", seq, live, streams:{en:[...], ar:[...]}}
 *          -> {"type":"diff", seq, base, live, streams:{...}}   (changed only)
 *          -> {"type":"ping"}
 *   client -> {"type":"resync"}                                (nothing else)
 *
 * Two contract details from app/captions.py that a client gets wrong by
 * default:
 *
 * 1. **Paragraph ids are globally unique, not aligned across streams.** The
 *    same id never appears in both `en` and `ar`, so the two streams are
 *    kept in separate maps. Ids ascend with time, which is what lets a
 *    paragraph be inserted in the right place without the server sending any
 *    ordering information.
 * 2. **Trimming is invisible in a diff.** When the 200-paragraph cap drops
 *    the oldest, nothing says "deleted" - they just stop appearing. So a
 *    snapshot is authoritative: any id missing from one is dropped locally.
 *
 * `base` is the sequence number a diff was computed from. If it is ahead of
 * what this client has, a message was lost and the client asks for a full
 * snapshot rather than quietly rendering a hole.
 *
 * No framework, no build step (the dependency list). Served as-is.
 */

(function () {
  'use strict';

  /* The two output languages, which is what the audience page's buttons
   * choose between. */
  var LANGS = ['en', 'ar'];
  /* Every stream the server sends. `auto` is the translation-only stream the
   * projector shows: Arabic speech in English, English speech in Arabic,
   * never the original. Its paragraphs carry their own `lang`/`dir` because
   * consecutive ones disagree. */
  var AUTO = 'auto';
  var STREAMS = ['en', 'ar', AUTO];
  var STORE = 'captions.';

  /* The most paragraphs a stream keeps in the DOM.
   *
   * Trimming is invisible in a diff, so a page only learns that the server
   * dropped the oldest paragraphs when a snapshot arrives - and a snapshot
   * only arrives on connect, on resync and when the operator changes a
   * setting. A talk that does none of those grows a node per paragraph for
   * as long as it runs: the 2026-10-24 session's 52 minutes left 977 <p>
   * elements on the projector against the 555 the server still held, and a
   * three-hour session would leave about 3,000.
   *
   * Comfortably above `captions.MAX_PARAGRAPHS` (200), so a paragraph is
   * only ever dropped here long after the server stopped being able to
   * update it. Measured at 0.1-1.1 ms per message either way, so this is
   * about memory on a laptop driving a projector for three hours, not about
   * speed.
   */
  var KEEP_PARAGRAPHS = 260;

  /* The server heartbeats every 5 s. Hearing nothing for this long means the
   * connection is gone, whatever the socket says about itself. Two missed
   * heartbeats plus room for a slow phone. */
  var SILENCE_LIMIT = 12000;
  /* How often a page says what it is actually rendering.
   *
   * **Why it reports at all.** On 2026-10-24 the projector was described as
   * showing the English original mid-talk, and nothing anywhere could say
   * what any page had on screen - so a day went into replaying logs to rule
   * things out, and the question "which stream was that screen rendering"
   * stayed unanswerable. A page now answers it every five seconds, and
   * the operator page shows the answer. */
  var HEALTH_MS = 5000;
  /* How often to check. Cheap, and the resolution the limit deserves. */
  var WATCHDOG_MS = 2000;

  /* localStorage throws in Safari private mode rather than returning null,
   * and a thrown exception here would take the captions down with it. */
  function load(key) {
    try { return window.localStorage.getItem(STORE + key); } catch (e) { return null; }
  }

  function save(key, value) {
    try { window.localStorage.setItem(STORE + key, value); } catch (e) { /* no-op */ }
  }

  /* -- the feed ---------------------------------------------------------- */

  function createFeed(handlers) {
    var seq = 0;
    var live = false;
    var status = 'connecting';
    /* Engine states worth showing the audience. `connecting` and `live` are
     * not here: a page that flickered on the first second of every session
     * would be worse than one that simply says "live". */
    var ENGINE_TROUBLE = { reconnecting: 'reconnecting', paused: 'paused' };
    var socket = null;
    var attempt = 0;
    var timer = null;
    var stopped = false;
    /* When anything last arrived from the server, caption or heartbeat. */
    var heardAt = 0;
    var watchdog = null;

    function socketUrl() {
      var scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      return scheme + '//' + window.location.host + '/ws';
    }

    function setStatus(next) {
      if (next === status) { return; }
      status = next;
      handlers.onStatus(status, live);
    }

    function applySnapshot(message) {
      STREAMS.forEach(function (lang) {
        var incoming = (message.streams && message.streams[lang]) || [];
        var present = Object.create(null);
        incoming.forEach(function (para) {
          present[para.id] = true;
          handlers.onParagraph(lang, para);
        });
        // Authoritative: whatever the server no longer lists is gone.
        handlers.onRetain(lang, present);
      });
    }

    function applyDiff(message) {
      STREAMS.forEach(function (lang) {
        var changed = (message.streams && message.streams[lang]) || [];
        changed.forEach(function (para) { handlers.onParagraph(lang, para); });
      });
    }

    function handle(raw) {
      var message;
      try { message = JSON.parse(raw); } catch (e) { return; }
      if (!message || typeof message !== 'object') { return; }

      /* Any message at all is proof of life, the heartbeat included. */
      heardAt = Date.now();

      if (message.type === 'ping') { return; }

      /* "Tell me everything about yourself." Asked for once, when the
       * server has decided this page is showing the wrong language. The
       * answer is the only large thing this socket ever carries upward, and
       * it is what makes a fault readable the next morning. */
      if (message.type === 'diagnose') {
        if (handlers.onDiagnose) {
          var snapshot = handlers.onDiagnose();
          if (snapshot) {
            snapshot.type = 'diagnostic';
            request(snapshot);
          }
        }
        return;
      }

      /* "Reload yourself." The operator's mid-talk fix for a projector
       * nobody can reach: the session keeps running, so this page comes
       * straight back and is handed a fresh snapshot by its new socket.
       * Before this the only remedy was Stop + Start, which ends the talk
       * to fix a browser. */
      if (message.type === 'reload') {
        stopped = true;             // do not reconnect out from under it
        window.location.reload();
        return;
      }

      if (message.type === 'snapshot') {
        applySnapshot(message);
      } else if (message.type === 'diff') {
        if (typeof message.base === 'number' && message.base > seq) {
          // A message went missing. Do not paint a gap - ask for everything.
          request({ type: 'resync' });
          return;
        }
        applyDiff(message);
      } else {
        return;
      }

      seq = typeof message.seq === 'number' ? message.seq : seq;
      live = !!message.live;
      /* The operator's main-screen choice rides on every snapshot and diff,
       * so a projector picks it up on connect and on every change without
       * anyone touching that browser. */
      if (message.screen && handlers.onScreen) { handlers.onScreen(message.screen); }
      /* Two different things can be wrong, and they are not the same thing:
       * this page's own socket, and the server's connection to the engine.
       * `message.status` is the second one (the resilience design) - when the
       * engine is reconnecting the talk has not ended, so `live` stays true
       * and the chip shows the engine's trouble rather than going to
       * "not live". */
      setStatus(live ? (ENGINE_TROUBLE[message.status] || 'live') : 'idle');
      handlers.onApplied(live);
    }

    function request(payload) {
      if (socket && socket.readyState === 1) {
        try { socket.send(JSON.stringify(payload)); } catch (e) { /* closing */ }
      }
    }

    function retry() {
      if (stopped || timer) { return; }
      // 1s, 2s, 4s, 8s, then 15s for as long as it takes (the design brief).
      var delay = Math.min(15000, 1000 * Math.pow(2, attempt));
      attempt += 1;
      timer = window.setTimeout(function () { timer = null; connect(); }, delay);
    }

    /* **Why this exists.** On run9 the laptop's Wi-Fi was switched off and
     * the phone's socket died without either end sending a FIN. The phone sat
     * in `OPEN` believing it was connected, `onclose` never fired, and the
     * page showed the same frozen line for the rest of the talk while the
     * laptop - on loopback, which never went away - carried on fine.
     *
     * A dead socket is indistinguishable from a quiet one, so silence is the
     * only signal available: the server heartbeats every 5 s, and hearing
     * nothing for `SILENCE_LIMIT` means the connection is gone whatever the
     * socket claims. Closing it by hand is what produces the `onclose` the
     * reconnect path is waiting for. */
    function checkAlive() {
      if (stopped || !socket || socket.readyState !== 1) { return; }
      if (Date.now() - heardAt < SILENCE_LIMIT) { return; }
      setStatus('reconnecting');
      try { socket.close(); } catch (e) { /* already gone */ }
      /* Some browsers will not fire `onclose` for a socket whose network has
       * vanished. Drop the reference and reconnect regardless. */
      socket = null;
      retry();
    }

    /* A phone coming back - the radio returning, or the screen waking - is
     * better evidence than any timer, so take the shortcut when it arrives. */
    function wakeUp() {
      if (stopped) { return; }
      if (socket && socket.readyState === 1
          && Date.now() - heardAt < SILENCE_LIMIT) {
        return;                       // still healthy, nothing to do
      }
      if (socket) {
        try { socket.close(); } catch (e) { /* already gone */ }
        socket = null;
      }
      if (timer) { window.clearTimeout(timer); timer = null; }
      attempt = 0;                    // it just came back; do not wait 15 s
      setStatus('reconnecting');
      connect();
    }

    function connect() {
      if (stopped) { return; }
      setStatus(seq === 0 ? 'connecting' : 'reconnecting');
      var next;
      try {
        next = new WebSocket(socketUrl());
      } catch (e) {
        retry();
        return;
      }
      socket = next;

      next.onopen = function () {
        attempt = 0;
        heardAt = Date.now();
        /* Say what is on screen straight away rather than up to five
         * seconds later: a reconnect is exactly when an operator is looking
         * at the operator page wondering what the projector is doing. */
        reportHealth();
        // The server opens with a full snapshot, so there is nothing to ask
        // for here: reconnecting is itself the resync, and it brings the page
        // up to the captions as they are now rather than where it left off.
      };
      next.onmessage = function (event) { handle(event.data); };
      next.onclose = function () {
        if (socket === next) { socket = null; }
        setStatus('reconnecting');
        retry();
      };
      next.onerror = function () { /* onclose always follows; handled there */ };
    }

    /* A locked phone suspends the socket and the close event may only arrive
     * when the screen comes back - if it arrives at all. The old version of
     * this handler returned early whenever `readyState <= 1`, which is
     * precisely the frozen-but-OPEN socket run9 was stuck on, so it never
     * helped. `wakeUp` judges by silence instead. */
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') { wakeUp(); }
    });
    /* The radio coming back. Not every browser fires it, and it is not
     * trusted on its own - the watchdog still runs - but when it does arrive
     * it is the earliest possible signal. */
    window.addEventListener('online', wakeUp);

    /* What this page is actually rendering, asked of the page itself rather
     * than remembered here: the handler reads the DOM, because a pane that
     * is visible when it should not be is exactly the fault that could not
     * be seen from the outside. Optional - the audience page does not
     * report, since nobody is driving it from the operator page. */
    var health = null;
    function reportHealth() {
      if (stopped || !handlers.onHealth) { return; }
      var now = handlers.onHealth();
      if (!now) { return; }
      now.type = 'health';
      now.seq = seq;
      now.visible = document.visibilityState !== 'hidden';
      request(now);
    }

    /* **A page that threw is a page that stopped updating**, and until now
     * that left no trace anywhere: the socket stays open, the heartbeat
     * keeps arriving, and the room reads the same line for the rest of the
     * talk. Both handlers report the message, the file and the line - which
     * are JavaScript strings, not captions - and the server writes them
     * down as page errors. */
    function reportError(kind, message, source, line) {
      if (stopped) { return; }
      request({
        type: 'error', kind: kind,
        message: String(message || '').slice(0, 300),
        source: String(source || '').slice(0, 200),
        line: typeof line === 'number' ? line : null
      });
    }

    window.addEventListener('error', function (event) {
      reportError('error', event.message, event.filename, event.lineno);
    });
    window.addEventListener('unhandledrejection', function (event) {
      var reason = event.reason;
      reportError('unhandledrejection',
                  reason && reason.message ? reason.message : reason, '', null);
    });

    watchdog = window.setInterval(checkAlive, WATCHDOG_MS);
    if (handlers.onHealth) {
      health = window.setInterval(reportHealth, HEALTH_MS);
    }
    connect();

    return {
      resync: function () { request({ type: 'resync' }); },
      /* For the tests, and for the first report after a reconnect: say so
       * now rather than waiting out the interval. */
      report: reportHealth,
      /* What this page has applied, which the diagnostic reports and the
       * server compares against its own. */
      appliedSeq: function () { return seq; },
      status: function () { return status; },
      isLive: function () { return live; },
      /* For tests: how long since anything was heard from the server. */
      silentFor: function () { return Date.now() - heardAt; },
      stop: function () {
        stopped = true;
        if (timer) { window.clearTimeout(timer); timer = null; }
        if (watchdog) { window.clearInterval(watchdog); watchdog = null; }
        if (health) { window.clearInterval(health); health = null; }
        if (socket) { try { socket.close(); } catch (e) { /* already gone */ } }
      }
    };
  }

  window.CaptionFeed = {
    create: createFeed, langs: LANGS, streams: STREAMS, auto: AUTO,
    load: load, save: save
  };

  /* -- paragraph rendering ---------------------------------------------- */

  /* One <p> per paragraph, each holding a final span and a partial span.
   * Nothing is ever re-rendered: an update writes to one of those two text
   * nodes. That is what keeps the page from flickering 10 times a second,
   * and it keeps the browser's text selection alive while captions arrive. */
  /* Drop the oldest ids beyond `KEEP_PARAGRAPHS`, youngest kept.
   *
   * Shared by both renderers because both grow the same way. Takes the ids
   * it holds and a remover, and returns nothing: the caller owns its own
   * bookkeeping.
   */
  function prune(ids, remove) {
    if (ids.length <= KEEP_PARAGRAPHS) { return; }
    ids.sort(function (a, b) { return Number(a) - Number(b); });
    ids.slice(0, ids.length - KEEP_PARAGRAPHS).forEach(remove);
  }

  function createRenderer(container) {
    var nodes = Object.create(null);

    function place(paragraph, id) {
      var last = container.lastElementChild;
      if (!last || Number(last.dataset.id) < id) {
        container.appendChild(paragraph);
        return;
      }
      var children = container.children;
      for (var i = 0; i < children.length; i += 1) {
        if (Number(children[i].dataset.id) > id) {
          container.insertBefore(paragraph, children[i]);
          return;
        }
      }
      container.appendChild(paragraph);
    }

    function drop(id) {
      var node = nodes[id];
      if (!node) { return; }
      if (node.element.parentNode) {
        node.element.parentNode.removeChild(node.element);
      }
      delete nodes[id];
    }

    return {
      upsert: function (para) {
        var node = nodes[para.id];
        if (!node) {
          var element = document.createElement('p');
          element.dataset.id = para.id;
          var finalSpan = document.createElement('span');
          var partialSpan = document.createElement('span');
          partialSpan.className = 'partial';
          element.appendChild(finalSpan);
          element.appendChild(partialSpan);
          node = { element: element, final: finalSpan, partial: partialSpan };
          nodes[para.id] = node;
          place(element, para.id);
          prune(Object.keys(nodes), drop);
        }
        /* A notice is the server telling the room something was missed, not
         * words anyone said. Set once, on creation - a paragraph never stops
         * being one. */
        if (para.notice) { node.element.className = 'notice'; }
        var finalText = para.final || '';
        var partialText = para.partial || '';
        if (node.final.textContent !== finalText) { node.final.textContent = finalText; }
        if (node.partial.textContent !== partialText) { node.partial.textContent = partialText; }
      },

      retain: function (present) {
        Object.keys(nodes).forEach(function (id) {
          if (present[id]) { return; }
          drop(id);
        });
      },

      count: function () { return Object.keys(nodes).length; },

      /* Last N paragraphs, newest last. Used by the big screen. */
      tail: function (limit) {
        return Object.keys(nodes)
          .map(Number)
          .sort(function (a, b) { return a - b; })
          .slice(-limit);
      },

      node: function (id) { return nodes[id]; }
    };
  }

  window.CaptionFeed.renderer = createRenderer;

  /* -- audience page ----------------------------------------------------- */

  var TEXT = {
    en: {
      connecting: 'connecting',
      reconnecting: 'reconnecting',
      live: 'live',
      paused: 'paused',
      idle: 'not live',
      disclaimer: 'Live auto-translation. May contain mistakes.',
      waiting: 'No session live right now. Captions start when the talk does.',
      jump: 'Jump to live'
    },
    ar: {
      connecting: 'جارٍ الاتصال',
      reconnecting: 'إعادة الاتصال',
      live: 'مباشر',
      paused: 'مُلبَث',
      idle: 'غير مباشر',
      disclaimer: 'ترجمة آلية مباشرة، قد تحتوي على أخطاء.',
      waiting: 'لا توجد جلسة مباشرة الآن. تبدأ الترجمة مع بداية الحديث.',
      jump: 'انتقل إلى المباشر'
    },
    /* The translation-only column has no single language - it alternates -
     * so its own chrome says everything twice. */
    auto: {
      waiting: 'Captions start when the talk does. '
        + 'تبدأ الترجمة مع بداية الحديث.',
      jump: 'Jump to live'
    }
  };

  var MIN_SIZE = 16;
  var MAX_SIZE = 44;
  var SIZE_STEP = 2;

  /* How close to the bottom still counts as "following the talk". Generous,
   * because a thumb resting on a phone drifts a few pixels. */
  var FOLLOW_SLACK = 90;

  function mountAudience() {
    var main = document.getElementById('streams');
    var stateChip = document.getElementById('state');
    var disclaimer = document.getElementById('disclaimer');

    var streams = {};
    var columns = {};
    var jumps = {};
    var renderers = {};
    var following = {};
    var notes = {};

    STREAMS.forEach(function (lang) {
      streams[lang] = document.getElementById('stream-' + lang);
      columns[lang] = document.getElementById('column-' + lang);
      jumps[lang] = document.getElementById('jump-' + lang);
      /* The translation-only column reveals word by word and sets a
       * direction per paragraph, so it has its own renderer. Everything
       * else about the column - scroll container, auto-follow, its own
       * button - is identical. */
      renderers[lang] = lang === AUTO
        ? window.CaptionAuto.create(streams[lang], {
            onPaint: function () {
              if (following[AUTO]) { scrollToLive(AUTO); }
            }
          })
        : createRenderer(streams[lang]);
      following[lang] = true;
      /* Each stream is its own scroll container, so this fires for one
       * language only: scrolling the English back to re-read a line must not
       * make the Arabic stop following the talk. */
      streams[lang].addEventListener('scroll', function () {
        following[lang] = isNearBottom(streams[lang]);
        updateJump();
      }, { passive: true });
      jumps[lang].addEventListener('click', function () {
        scrollToLive(lang);
        updateJump();
      });
    });

    var view = pickView();
    var size = pickSize();
    var live = false;

    function pickView() {
      var stored = load('view');
      if (stored === 'en' || stored === 'ar' || stored === 'both'
          || stored === AUTO) { return stored; }
      var language = (navigator.language || 'en').toLowerCase();
      return language.indexOf('ar') === 0 ? 'ar' : 'en';
    }

    function pickSize() {
      var stored = parseInt(load('size'), 10);
      if (!stored || stored < MIN_SIZE || stored > MAX_SIZE) { return 22; }
      return stored;
    }

    function words() { return TEXT[view === 'ar' ? 'ar' : 'en']; }

    function isNearBottom(element) {
      return element.scrollHeight - element.scrollTop - element.clientHeight
        <= FOLLOW_SLACK;
    }

    function visibleLangs() {
      return view === 'both' ? LANGS : [view];
    }

    function scrollToLive(lang) {
      var element = streams[lang];
      element.scrollTop = element.scrollHeight;
      following[lang] = true;
    }

    /* One button per column, each answering for its own language. The old
     * single page-level button showed whenever *either* visible column was
     * behind and scrolled *both* back to the live edge - so catching the
     * English up threw away the place the reader had scrolled to in the
     * Arabic. A column that is still following the talk shows no button. */
    function updateJump() {
      var shown = visibleLangs();
      STREAMS.forEach(function (lang) {
        var visible = shown.indexOf(lang) !== -1;
        jumps[lang].hidden = !(visible && !following[lang]);
      });
    }

    function applySize() {
      document.documentElement.style.setProperty('--size', size + 'px');
    }

    function applyView() {
      main.classList.toggle('both', view === 'both');
      var shown = visibleLangs();
      STREAMS.forEach(function (lang) {
        // The column, not the stream: hiding the stream would leave that
        // column's jump button on screen with no captions under it.
        columns[lang].hidden = shown.indexOf(lang) === -1;
        /* Each button is labelled in the language of the column it sits over,
         * not in the language of the current view - in "Both" the two sit
         * side by side and one shared label could only be wrong for one. */
        jumps[lang].textContent = TEXT[lang].jump;
      });
      document.querySelectorAll('.controls button[data-view]').forEach(function (button) {
        button.setAttribute('aria-pressed', String(button.dataset.view === view));
      });
      disclaimer.textContent = words().disclaimer;
      disclaimer.setAttribute('dir', view === 'ar' ? 'rtl' : 'ltr');
      renderStatus();
      renderNotes();
      // A stream that was hidden could not scroll while it was hidden.
      shown.forEach(function (lang) { if (following[lang]) { scrollToLive(lang); } });
      updateJump();
    }

    function renderStatus() {
      var status = feed ? feed.status() : 'connecting';
      stateChip.textContent = words()[status] || status;
      stateChip.className = status;
    }

    /* The spec's "No session live right now" line, shown in the stream body
     * where the captions would be - not as a banner, so it vanishes the
     * moment the first paragraph lands. */
    function renderNotes() {
      STREAMS.forEach(function (lang) {
        var needed = !live && renderers[lang].count() === 0;
        var note = notes[lang];
        if (needed && !note) {
          note = document.createElement('p');
          note.className = 'note';
          notes[lang] = note;
          streams[lang].appendChild(note);
        }
        if (needed) {
          note.textContent = TEXT[lang].waiting;
        } else if (note) {
          if (note.parentNode) { note.parentNode.removeChild(note); }
          notes[lang] = null;
        }
      });
    }

    var feed = createFeed({
      /* This page reports too, and it is not an afterthought.
       *
       * It is the **most likely** thing to have been in front of the room on
       * 2026-10-24: it draws with the plain renderer, so its text arrives
       * grey and turns white exactly as described; it defaults to English on
       * an English-locale browser, with no Auto default at all; and it
       * ignores the operator's main-screen setting completely. A page like
       * that looks perfect while Arabic is being spoken and shows the
       * original the moment the talk switches to English - which is the
       * report, in full, with nothing having "changed" but the speech.
       *
       * The operator page lists these separately from the projectors and
       * does not call them wrong: this page is *supposed* to follow its own
       * reader. Being able to see that one is open is the whole point. */
      onHealth: function () {
        return {
          rendered: view,
          pinned: false,
          page: 'audience',
          /* The same check as the projector's, for the same reason: a
           * browser translating the Arabic captions back into English
           * shows the room the words it just heard. Chrome marks the root
           * element and wraps what it rewrote in `<font>`. */
          translated: /\btranslated-(ltr|rtl)\b/.test(
            document.documentElement.className || '')
            || main.getElementsByTagName('font').length > 0,
          grey: streams[AUTO]
            ? streams[AUTO].querySelectorAll('.partial').length : 0,
          /* Where this page is. `/` in a log line is the answer to a whole
           * class of question about what was on the wall. */
          path: window.location.pathname + window.location.search
        };
      },
      onParagraph: function (lang, para) { renderers[lang].upsert(para); },
      onRetain: function (lang, present) { renderers[lang].retain(present); },
      onStatus: function () { renderStatus(); },
      onApplied: function (isLive) {
        live = isLive;
        renderNotes();
        renderStatus();
        visibleLangs().forEach(function (lang) {
          if (following[lang]) { scrollToLive(lang); }
        });
        updateJump();
      }
    });

    document.querySelectorAll('.controls button[data-view]').forEach(function (button) {
      button.addEventListener('click', function () {
        view = button.dataset.view;
        save('view', view);
        applyView();
      });
    });

    document.getElementById('bigger').addEventListener('click', function () {
      size = Math.min(MAX_SIZE, size + SIZE_STEP);
      save('size', size);
      applySize();
    });

    document.getElementById('smaller').addEventListener('click', function () {
      size = Math.max(MIN_SIZE, size - SIZE_STEP);
      save('size', size);
      applySize();
    });

    applySize();
    applyView();
  }

  if (document.body.dataset.page === 'audience') {
    mountAudience();
  }
}());
