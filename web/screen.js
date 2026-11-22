/* Big-screen view: `/screen`.
 *
 * Defaults to `auto` - the translation-only stream - because the room is
 * mixed and the one thing nobody on the floor needs is the words they can
 * already hear. `?lang=auto|en|ar|both` and `?size=s|m|l|xl` override the
 * operator's setting, which is otherwise pushed down this page's own socket
 * so the projector browser never has to be touched.
 *
 * It reuses `CaptionFeed` from app.js so there is one implementation of the
 * snapshot/diff protocol in the project, and `CaptionAuto` for the reveal,
 * so the audience page's Auto column behaves identically.
 *
 * Older paragraphs in the single-language views are hidden rather than
 * removed: the server may still update a paragraph that has scrolled out of
 * the window, and a hidden node accepts that at no cost.
 */

(function () {
  'use strict';

  var MAX_LINES = 8;

  /* How much of the newest visible text is sampled to decide which script
   * is on screen. Long enough that an embedded English term inside an
   * Arabic line cannot swing it, short enough to be the *current* text
   * rather than the last minute of it. */
  var SCRIPT_SAMPLE = 80;

  /* How long the screen has to have nothing left to reveal before the dots
   * appear. Without this they flash on in every gap between words - the
   * reveal settles many times a second - and a blinking indicator is exactly
   * the "jumpy" the dots exist to avoid.
   *
   * Set above the engine's own rhythm, from the 2026-10-17 logs: translation
   * chunks arrive about every 2 s and take ~0.45 s to reveal, so continuous
   * speech leaves roughly 1.5 s idle between them. At 900 ms the dots came up
   * in every one of those gaps - 14 toggles in 20 s of replay. Above that
   * rhythm they stay down while the talk is flowing and appear only when
   * translation has genuinely stalled, which is the p90 gap of 3.4 s and the
   * 8 s one the second dry run hit. */
  var LISTENING_AFTER_MS = 1800;
  var SIZES = ['s', 'm', 'l', 'xl'];
  var VIEWS = ['auto', 'en', 'ar', 'both'];

  var params = new URLSearchParams(window.location.search);

  /* A URL wins over the operator, and keeps winning: this is the testing
   * hatch, and a setting that silently got overridden by the operator halfway
   * through a check would be worse than no hatch at all. */
  var forcedView = VIEWS.indexOf(params.get('lang')) !== -1
    ? params.get('lang') : null;
  var forcedSize = SIZES.indexOf(params.get('size')) !== -1
    ? params.get('size') : null;
  /* `?logo=1` / `?logo=0`. Absent means "whatever the operator set". */
  var forcedLogo = params.get('logo') === null
    ? null : params.get('logo') === '1';
  /* `?glow=1` / `?glow=0`, the same hatch for the background wash. Pinning
   * it off is how the "one surface colour" check gets a plain black screen
   * to measure without an operator having to touch the setting. */
  var forcedGlow = params.get('glow') === null
    ? null : params.get('glow') === '1';

  /* `?card=1` holds the title card up and `?card=0` holds it down, both
   * without a session running. The card is otherwise only ever on screen
   * during the gap between the first word and its translation, which is a
   * few seconds in the middle of a talk and no way to check how it looks on
   * the projector that is actually in the room. Same hatch as `?lang=`: a
   * URL wins over the state, and keeps winning. */
  var forcedCard = params.get('card') === null
    ? null : params.get('card') === '1';

  var requested = parseInt(params.get('lines'), 10);
  var lines = Math.max(1, Math.min(MAX_LINES, requested || 3));

  var view = forcedView || 'auto';
  var size = forcedSize || 'l';
  var logo = forcedLogo === null ? false : forcedLogo;
  /* On unless told otherwise, matching the server's default: the room
   * should not have to wait for a setting to arrive before the screen
   * looks like the poster it just replaced. */
  var glow = forcedGlow === null ? true : forcedGlow;
  var footer = true;
  var labels = true;
  var credit = '';

  /* The waiting screen's words. Defaults live on the server - these are
   * only what the page shows in the fraction of a second before the first
   * snapshot lands, which is why they are empty rather than a second copy
   * of the text in `broadcast.py`. */
  var wait = {
    eyebrow: '', headline: '', highlight: true,
    subline: '', pill: '', cta: '', note: '',
    ext: 'svg', tag: ''
  };

  var panes = {
    auto: document.getElementById('screen-auto'),
    en: document.getElementById('screen-en'),
    ar: document.getElementById('screen-ar')
  };
  var screenmark = document.getElementById('screenmark');
  var listening = document.getElementById('listening');
  var titlecard = document.getElementById('titlecard');
  var waitscreen = document.getElementById('waitscreen');
  var waitParts = {
    eyebrow: document.getElementById('wait-eyebrow'),
    headline: document.getElementById('wait-headline'),
    subline: document.getElementById('wait-sub'),
    pill: document.getElementById('wait-pill'),
    cta: document.getElementById('wait-cta'),
    note: document.getElementById('wait-note-text'),
    join: document.getElementById('wait-join'),
    qr: document.getElementById('wait-qr')
  };
  var titlecredit = document.getElementById('titlecard-credit');
  var screenfooter = document.getElementById('screenfooter');
  var linkdot = document.getElementById('linkdot');

  var live = false;
  var status = 'connecting';
  var settled = true;
  /* When the reveal last ran out of work, or 0 while it still has some. */
  var settledAt = Date.now();

  var auto = window.CaptionAuto.create(panes.auto, {
    onIdle: function (isSettled) {
      if (isSettled === settled) { return; }
      settled = isSettled;
      settledAt = isSettled ? Date.now() : 0;
      paintChrome();
    }
  });
  var plain = {
    en: window.CaptionFeed.renderer(panes.en),
    ar: window.CaptionFeed.renderer(panes.ar)
  };

  function visiblePanes() {
    if (view === 'both') { return ['en', 'ar']; }
    return [view];
  }

  function applyView() {
    document.body.dataset.view = view;
    document.body.dataset.size = size;
    document.body.dataset.logo = logo ? '1' : '0';
    document.body.dataset.glow = glow ? '1' : '0';
    document.body.dataset.footer = footer ? '1' : '0';
    document.body.dataset.labels = labels ? '1' : '0';
    screenmark.hidden = !logo;
    screenfooter.hidden = !footer;
    titlecredit.textContent = credit;
    titlecredit.hidden = !credit;
    applyWait();
    Object.keys(panes).forEach(function (key) {
      panes[key].hidden = visiblePanes().indexOf(key) === -1;
    });
    showLastN();
    paintChrome();
  }

  /* The waiting screen's text. Rebuilt whole rather than patched: it changes
   * when an operator types, which is a handful of times before a talk, and
   * "set it to exactly this" is one less thing to get wrong than a diff. */
  function applyWait() {
    waitParts.eyebrow.textContent = wait.eyebrow;
    waitParts.pill.textContent = wait.pill;
    waitParts.cta.textContent = wait.cta;
    waitParts.note.textContent = wait.note;
    paintHeadline();
    paintSubline();
    fitWaitText();

    /* The card is always there. There are three places a code can come
     * from - the organizer's own file, the operator's link, or the default
     * link - and the server has already picked one, so there is no state
     * in which this column has nothing to show.
     *
     * The fingerprint is of whichever source is live, and it is a hash:
     * this page is never told the invite URL. It rides in the query string
     * so that changing the code changes the image's address, which is
     * cache-busting no header can get subtly wrong. */
    var src = '/screen/qr.' + (wait.ext === 'png' ? 'png' : 'svg')
      + '?v=' + encodeURIComponent(wait.tag);
    if (waitParts.qr.getAttribute('src') !== src) {
      waitParts.qr.setAttribute('src', src);
    }
  }

  /* Keep a line of the poster's copy on one line by shrinking it, never by
   * wrapping. Both the headline and the subline go through this.
   *
   * The stylesheet's size is the largest either may be - chosen so the
   * default words fill the left column at 1920x1080. Longer ones would run
   * into the QR card, so they are scaled down to fit the column they are
   * in. Never scaled up: a two-word headline should not balloon to fill the
   * screen, it should sit at the size the design was drawn at.
   *
   * Measured rather than estimated, because the answer depends on the
   * words, the face and the viewport together. `scrollWidth` on a `nowrap`
   * block is the width of its widest line whatever the box around it is
   * doing - which is what makes this work for the subline's two lines as
   * well as the headline's one: both shrink by the same ratio, so the
   * typed break stays where it was typed.
   */
  function fitLines(node, text) {
    /* Back to the stylesheet's size first, or each call would measure the
     * result of the last one and ratchet the type down. */
    node.style.removeProperty('font-size');
    if (!text) { return; }
    var room = node.parentNode.clientWidth;
    var line = node.scrollWidth;
    if (!room || !line || line <= room) { return; }
    var base = parseFloat(window.getComputedStyle(node).fontSize);
    node.style.fontSize = (base * room / line) + 'px';
  }

  function fitWaitText() {
    fitLines(waitParts.headline, wait.headline);
    fitLines(waitParts.subline, wait.subline);
  }

  /* The headline, with its last word in the accent when the operator has
   * left that on. One field and a flag rather than two fields: a headline
   * cannot disagree with itself about what its own last word is. */
  function paintHeadline() {
    var node = waitParts.headline;
    node.textContent = '';
    if (!wait.headline) { return; }
    if (!wait.highlight) { node.textContent = wait.headline; return; }
    var cut = wait.headline.lastIndexOf(' ');
    if (cut !== -1) {
      node.appendChild(document.createTextNode(wait.headline.slice(0, cut + 1)));
    }
    var span = document.createElement('span');
    span.className = 'accent';
    span.textContent = wait.headline.slice(cut + 1);
    node.appendChild(span);
  }

  /* The subline, with the break the operator typed.
   *
   * Text nodes and `<br>` elements, **never** `innerHTML`: this string was
   * typed into a field on the operator page and goes on a projector, and
   * the one piece of markup it is allowed to produce is the break. The
   * server has already collapsed everything else - see `tidy_subline` in
   * `broadcast.py` - so what is left here is at most two lines.
   */
  function paintSubline() {
    var node = waitParts.subline;
    node.textContent = '';
    var parts = String(wait.subline || '').split('\n');
    for (var i = 0; i < parts.length; i += 1) {
      if (i) { node.appendChild(document.createElement('br')); }
      node.appendChild(document.createTextNode(parts[i]));
    }
  }

  function showLastN() {
    ['en', 'ar'].forEach(function (lang) {
      var children = panes[lang].children;
      var cutoff = children.length - lines;
      for (var i = 0; i < children.length; i += 1) {
        children[i].hidden = i < cutoff;
      }
    });
  }

  function hasText() {
    if (view === 'auto') { return auto.count() > 0; }
    return visiblePanes().some(function (lang) {
      return plain[lang].count() > 0;
    });
  }

  function paintChrome() {
    /* Three states, and the difference between the last two is what the
     * room is being told.
     *
     * Nothing running - before Start, after Stop - is the waiting screen:
     * the organizer's poster with the join code, which is the one thing
     * worth a projector while people are arriving or leaving.
     *
     * Running but with nothing to show yet - the gap between the first word
     * and its translation - keeps the small title card. A room that has
     * just heard the talk begin should not be shown a "come and join us"
     * poster for the few seconds the engine takes to catch up.
     *
     * Both cover the captions rather than clearing them, so a session that
     * resumes still has its last line underneath. */
    var carded = forcedCard === null ? (live && !hasText()) : forcedCard;
    var waiting = !live && !carded;
    waitscreen.classList.toggle('show', waiting);
    titlecard.classList.toggle('show', carded);

    /* Dots whenever the room is being heard and there is nothing left to
     * reveal. They go the moment a word appears, which is what makes a long
     * translation gap read as "listening" rather than "stuck", and they
     * never sit under the title card - that already says the same thing. */
    listening.hidden = !(live && settled && !carded && !waiting
                         && settledAt && Date.now() - settledAt >= LISTENING_AFTER_MS);

    /* The only thing a reconnect may put on a projector. */
    linkdot.hidden = !(status === 'reconnecting' || status === 'connecting');
  }

  /* What is on screen, measured rather than remembered.
   *
   * **This is the answer to 2026-10-24.** The projector was described as
   * showing the English original half an hour into a talk, and nothing could
   * say which pane was actually visible - so the fault had to be chased
   * through logs that could only rule things out. `rendered` is read off the
   * DOM, so it is what the room is looking at and not what this page
   * intended; `pinned` says a URL overrode the operator, which is the one
   * state that looks exactly like a bug; and `grey` counts non-final spans
   * inside the translation-only pane, which should be impossible - no
   * translation token is ever non-final and `auto` has no partials - so a
   * number there names the thing that was reported rather than leaving it to
   * be reconstructed afterwards.
   */
  function renderedView() {
    var shown = Object.keys(panes).filter(function (key) {
      return !panes[key].hidden;
    });
    if (shown.length === 0) { return 'none'; }
    if (shown.length === 1) { return shown[0]; }
    if (shown.length === 2 && shown.indexOf('en') !== -1
        && shown.indexOf('ar') !== -1) { return 'both'; }
    return 'mixed';
  }

  /* Is the browser translating this page?
   *
   * `translate="no"` and the meta tell it not to, and that is the fix - this
   * is the check that the fix held, on the machine that matters rather than
   * in the markup. An operator who does not read Arabic may have "always
   * translate Arabic" set, and a caption screen is the worst possible place
   * for it: once a stretch of Arabic fills the pane the browser decides the
   * page is Arabic and translates it **back into English in place**, so the
   * room reads the words it has just heard. That is the 2026-10-24 report,
   * in full, including why it began minutes after the talk changed language.
   *
   * Chrome and Edge mark the root element `translated-ltr` or
   * `translated-rtl` and wrap the text they rewrite in `<font>` - neither of
   * which this page produces itself, so either one is proof. */
  function translated() {
    var root = document.documentElement;
    if (/\btranslated-(ltr|rtl)\b/.test(root.className || '')) { return true; }
    return panes.auto.getElementsByTagName('font').length > 0
      || panes.en.getElementsByTagName('font').length > 0
      || panes.ar.getElementsByTagName('font').length > 0;
  }

  /* **What the room can actually read.**
   *
   * The script of the last stretch of visible text in the pane that is on
   * screen, measured off the rendered DOM - not off the data the page was
   * sent. That difference is the whole value of it: it is true whatever went
   * wrong, whether the server sent the wrong thing, the page rendered the
   * wrong pane, or a browser rewrote the words after they landed. The server
   * compares it with the language of its own newest `auto` paragraph and
   * says so when the two disagree for longer than translation can explain.
   *
   * Counts of letters, never the letters: `share` is a proportion and
   * `script` is one of three words. No caption text leaves this function.
   */
  function visibleScript() {
    var pane = panes[renderedView() === 'both' ? 'en' : renderedView()];
    if (!pane) { return {script: 'none', para: -1, chars: 0}; }
    var blocks = pane.children;
    var text = '';
    var para = -1;
    for (var i = blocks.length - 1; i >= 0 && text.length < SCRIPT_SAMPLE; i -= 1) {
      if (blocks[i].hidden) { continue; }
      var block = blocks[i].textContent || '';
      if (!block.trim()) { continue; }
      if (para === -1) { para = Number(blocks[i].dataset.id); }
      text = block.slice(-SCRIPT_SAMPLE) + text;
    }
    var arabic = 0, latin = 0;
    for (var j = 0; j < text.length; j += 1) {
      var code = text.charCodeAt(j);
      if (code >= 0x0600 && code <= 0x06FF) { arabic += 1; }
      else if ((code >= 65 && code <= 90) || (code >= 97 && code <= 122)) {
        latin += 1;
      }
    }
    if (!arabic && !latin) { return {script: 'none', para: para, chars: 0}; }
    return {
      script: arabic >= latin ? 'ar' : 'en',
      para: para,
      chars: arabic + latin
    };
  }

  function health() {
    var seen = visibleScript();
    return {
      rendered: renderedView(),
      pinned: forcedView !== null,
      translated: translated(),
      /* Where this page actually is. `/screen?lang=en` and `/` are the two
       * answers that explain a fault on their own, and neither is visible
       * from the server's side of a socket. */
      path: window.location.pathname + window.location.search,
      script: seen.script,
      para: seen.para,
      /* Which kind of page this is. The audience page reports too, and the
       * operator page has to be able to tell a projector that is disobeying
       * from a phone that is simply following its own reader. */
      page: 'screen',
      grey: panes.auto.querySelectorAll('.partial').length
    };
  }

  /* Everything this page can say about itself, asked for once when the
   * server has decided it is showing the wrong language.
   *
   * It is deliberately a dump: the reason the 2026-10-24 report cost a day
   * is that nobody had guessed in advance which fact would turn out to
   * matter. Attributes, counts, flags and this page's own address - no
   * caption text, and the script of what is on screen as a word rather than
   * as the words. */
  function diagnostic() {
    var root = document.documentElement;
    var seen = visibleScript();
    var visible = Object.keys(panes).filter(function (key) {
      return !panes[key].hidden;
    });
    return {
      path: window.location.pathname + window.location.search,
      lang: root.getAttribute('lang') || '',
      translate: root.getAttribute('translate') || '',
      classes: root.className || '',
      fonts: document.getElementsByTagName('font').length,
      panes: visible.join(','),
      view: view,
      pinned: forcedView !== null,
      seq: feed.appliedSeq(),
      visibility: document.visibilityState,
      pixel_ratio: window.devicePixelRatio,
      agent: navigator.userAgent,
      script: seen.script,
      para: seen.para,
      chars: seen.chars
    };
  }

  var feed = window.CaptionFeed.create({
    onHealth: health,
    onDiagnose: diagnostic,
    onParagraph: function (stream, para) {
      if (stream === window.CaptionFeed.auto) { auto.upsert(para); return; }
      if (plain[stream]) { plain[stream].upsert(para); }
    },
    onRetain: function (stream, present) {
      if (stream === window.CaptionFeed.auto) { auto.retain(present); return; }
      if (plain[stream]) { plain[stream].retain(present); }
    },
    onStatus: function (next) { status = next; paintChrome(); },
    onScreen: function (screen) {
      /* The operator's choice, unless this page was pinned with a URL. */
      var next = forcedView || screen.view;
      var nextSize = forcedSize || screen.size;
      var nextLogo = forcedLogo === null ? !!screen.logo : forcedLogo;
      var nextGlow = forcedGlow === null ? screen.glow !== false : forcedGlow;
      var nextFooter = screen.footer !== false;
      var nextLabels = screen.labels !== false;
      var nextCredit = screen.credit || '';
      var nextWait = {
        eyebrow: screen.eyebrow || '',
        headline: screen.headline || '',
        highlight: screen.highlight !== false,
        subline: screen.subline || '',
        pill: screen.pill || '',
        cta: screen.cta || '',
        note: screen.note || '',
        /* Which image to ask for, and a fingerprint of it. The invite link
         * itself is never on this message - only a hash. See
         * `BroadcastHub.public_screen`. */
        ext: screen.qr_ext === 'png' ? 'png' : 'svg',
        tag: screen.qr_tag || ''
      };
      if (VIEWS.indexOf(next) === -1) { next = view; }
      if (SIZES.indexOf(nextSize) === -1) { nextSize = size; }
      var waitChanged = Object.keys(nextWait).some(function (key) {
        return nextWait[key] !== wait[key];
      });
      if (next === view && nextSize === size && nextLogo === logo
          && nextGlow === glow && nextFooter === footer
          && nextLabels === labels
          && nextCredit === credit && !waitChanged) { return; }
      view = next;
      size = nextSize;
      logo = nextLogo;
      glow = nextGlow;
      footer = nextFooter;
      labels = nextLabels;
      credit = nextCredit;
      wait = nextWait;
      applyView();
    },
    onApplied: function (isLive) {
      live = isLive;
      showLastN();
      paintChrome();
    }
  });

  /* The fit depends on the viewport, so a projector that is plugged in,
   * rotated or switched to a different mode has to be re-measured. Once per
   * frame at most: a resize fires a stream of events and each one costs a
   * layout. The web font is the other thing that changes the answer - the
   * fallback face is a different width - so the first measurement is taken
   * again once it has landed. */
  var fitPending = false;
  window.addEventListener('resize', function () {
    if (fitPending) { return; }
    fitPending = true;
    window.requestAnimationFrame(function () {
      fitPending = false;
      fitWaitText();
    });
  });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(fitWaitText);
  }

  /* A code that will not load leaves the card empty rather than putting a
   * broken-image icon on a projector. The server only refuses a request for
   * the extension that is not live, which a connected page hears about on
   * the socket anyway - so this is for the tab that was left open across
   * that change, and it rights itself on the next snapshot. */
  waitParts.qr.addEventListener('error', function () {
    waitParts.qr.classList.add('missing');
  });
  waitParts.qr.addEventListener('load', function () {
    waitParts.qr.classList.remove('missing');
  });

  /* The dots are a function of elapsed time, so something has to re-ask.
   * Cheap, and it is the only timer on this page. */
  window.setInterval(paintChrome, 250);

  /* Exposed for the browser tests, which must not race the reveal. */
  window.CaptionScreen = {
    finishReveal: function () { auto.finishNow(); },
    /* What this page would tell the server it is showing, and the ability to
     * tell it now rather than at the next five-second tick. The tests read
     * the first and call the second. */
    health: health,
    diagnostic: diagnostic,
    report: function () { feed.report(); },
    view: function () { return view; },
    size: function () { return size; },
    logo: function () { return logo; },
    glow: function () { return glow; },
    footer: function () { return footer; },
    labels: function () { return labels; },
    credit: function () { return credit; },
    carded: function () { return titlecard.classList.contains('show'); },
    /* For the browser tests: what the headline measures, and what it was
     * allowed to be. */
    headline: function () {
      var node = waitParts.headline;
      return {
        text: node.textContent,
        line: node.scrollWidth,
        room: node.parentNode.clientWidth,
        size: parseFloat(window.getComputedStyle(node).fontSize),
        fitted: node.style.fontSize !== ''
      };
    },
    /* The same three numbers for the subline, plus where it broke: the
     * break is typed, so a test has to be able to see it as line boxes
     * rather than trust the string. */
    subline: function () {
      var node = waitParts.subline;
      var range = document.createRange();
      range.selectNodeContents(node);
      var rects = [];
      var boxes = range.getClientRects();
      for (var i = 0; i < boxes.length; i += 1) {
        if (boxes[i].width > 1) { rects.push(boxes[i].width); }
      }
      return {
        /* The string as it arrived, break and all. `textContent` is no use
         * for that - it skips `<br>` and runs the two lines together - so
         * `shown` is there for what is actually in the nodes and `text` is
         * what the break is being compared against. */
        text: wait.subline,
        shown: node.textContent,
        breaks: node.getElementsByTagName('br').length,
        widths: rects,
        line: node.scrollWidth,
        room: node.parentNode.clientWidth,
        size: parseFloat(window.getComputedStyle(node).fontSize),
        fitted: node.style.fontSize !== ''
      };
    },
    waiting: function () { return waitscreen.classList.contains('show'); },
    wait: function () {
      var copy = {};
      Object.keys(wait).forEach(function (key) { copy[key] = wait[key]; });
      return copy;
    },
    /* Why the dots are or are not up, without reading closures. */
    chrome: function () {
      return {
        live: live, status: status, settled: settled,
        settledFor: settledAt ? Date.now() - settledAt : null
      };
    }
  };

  applyView();
}());
