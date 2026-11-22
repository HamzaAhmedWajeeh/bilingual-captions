/* The translation-only view, shared by `/screen` and the audience page.
 *
 * `auto` is the stream nobody in the room can already hear: Arabic speech
 * shown in English, English speech shown in Arabic, and never the original.
 * Each paragraph arrives carrying its own `lang` and `dir`, because
 * consecutive ones disagree, and this file is what turns that into something
 * readable from the back of a room.
 *
 * Four behaviours, each answering a way the old view looked broken:
 *
 * 1. **Word-by-word reveal.** A translation chunk lands whole - the 2026-10-17
 *    dry run delivered 20-28 characters at a time, about every 2 s - and
 *    painting it in one go reads as a block dropping onto the screen. Words
 *    are released one at a time instead, fast enough that the reveal is never
 *    the thing you are waiting for: the backlog is always cleared inside
 *    `REVEAL_WINDOW_MS`, and anything older than `MAX_LAG_MS` is shown at once.
 * 2. **A listening indicator.** Translation lags its speech by about a second
 *    and can go 8 s between chunks (measured on the second dry-run log), so
 *    there are long stretches with nothing to paint. Three soft dots say the
 *    room is still being heard. They are not a spinner and they carry no text,
 *    because the projector is behind the speaker and must never compete.
 * 3. **Bottom anchoring.** The newest line sits at the bottom and older text
 *    moves up and out. Nothing above the newest line ever reflows, which is
 *    the thing that makes captions unreadable while they grow.
 * 4. **It never clears.** Silence leaves the last text up. Only a new session
 *    replaces it.
 */

(function () {
  'use strict';

  /* Clear whatever is waiting within this long. Under the 0.5 s that the
   * reveal is allowed to add, so the reveal is never the bottleneck. */
  var REVEAL_WINDOW_MS = 450;
  /* Fastest and slowest a single word may take. The ceiling keeps a lone
   * word from crawling in; the floor keeps a long backlog from strobing. */
  var MIN_STEP_MS = 30;
  var MAX_STEP_MS = 180;
  /* A word that has been waiting longer than this is simply shown, along
   * with everything behind it. The hard guarantee behind "the reveal must
   * never add more than ~0.5 s". */
  var MAX_LAG_MS = 600;
  /* The most paragraphs this view keeps. The same reasoning and the same
   * number as `KEEP_PARAGRAPHS` in app.js, which this file cannot see: both
   * sit above the server's 200-paragraph cap so a dropped node can never be
   * one the server is still able to update. */
  var KEEP_PARAGRAPHS = 260;

  function words(text) {
    /* Keep the separators: Arabic and English both need their own spacing,
     * and rejoining with a plain space would normalise away the leading
     * space that marks a word boundary. */
    return text.split(/(\s+)/).filter(function (piece) { return piece !== ''; });
  }

  function createAutoView(container, options) {
    options = options || {};
    var limit = options.limit || 0;            // 0 = no windowing
    var onIdle = options.onIdle || function () {};
    /* Called after every repaint. A scrolling container has to re-follow
     * here and nowhere else: the reveal grows the text *after* the diff was
     * applied, and content growing fires no scroll event, so a column that
     * only followed on arrival drifts a line or two off its own live edge
     * and never notices. */
    var onPaint = options.onPaint || function () {};

    var nodes = Object.create(null);
    var order = [];                            // paragraph ids, ascending
    var ticking = null;
    var lastStepAt = 0;
    var pendingSince = 0;

    function node(para) {
      var existing = nodes[para.id];
      if (existing) { return existing; }
      var element = document.createElement('p');
      element.className = 'auto-para';
      element.dataset.id = para.id;
      var made = { element: element, full: '', pieces: [], shown: 0 };
      nodes[para.id] = made;

      // Ids ascend with time, so position is found by id, never by arrival.
      var id = Number(para.id);
      var before = null;
      for (var i = 0; i < order.length; i += 1) {
        if (Number(order[i]) > id) { before = nodes[order[i]].element; break; }
      }
      order.push(para.id);
      order.sort(function (a, b) { return Number(a) - Number(b); });
      container.insertBefore(element, before);
      /* Trimming is invisible in a diff, so without this the pane keeps a
       * node for every paragraph of the whole talk - 155 after 52 minutes
       * on 2026-10-24, about 540 over three hours - and only ever sheds
       * them when a snapshot happens to arrive. The cap is well above the
       * server's own 200, so nothing is dropped that the server could
       * still update. */
      while (order.length > KEEP_PARAGRAPHS) {
        remove(order[0]);
      }
      return made;
    }

    function remove(id) {
      var entry = nodes[id];
      if (!entry) { return; }
      if (entry.element.parentNode) {
        entry.element.parentNode.removeChild(entry.element);
      }
      delete nodes[id];
      order.splice(order.indexOf(id), 1);
    }

    function dress(element, para) {
      /* Per paragraph, not per container: this view alternates direction. */
      var direction = para.dir || 'ltr';
      if (element.getAttribute('dir') !== direction) {
        element.setAttribute('dir', direction);
      }
      if (para.lang && element.getAttribute('lang') !== para.lang) {
        element.setAttribute('lang', para.lang);
      }
      if (para.notice) { element.classList.add('notice'); }
    }

    function markDirectionChanges() {
      /* A thin rule between two paragraphs that read in opposite
       * directions - the one place the eye needs help, and nowhere else.
       *
       * The rule also carries which way it turned, as `data-turn`
       * ("EN -> AR"). Written here rather than in CSS because only this loop
       * knows what came before; whether it is drawn is the stylesheet's
       * business, and the projector's toggle hides it without this changing. */
      var previous = null;
      var previousLang = null;
      order.forEach(function (id) {
        var element = nodes[id].element;
        var direction = element.getAttribute('dir');
        var lang = element.getAttribute('lang') || '';
        var changed = previous !== null && direction !== previous;
        element.classList.toggle('turn', changed);
        if (changed && previousLang && lang) {
          element.dataset.turn =
            previousLang.toUpperCase() + ' → ' + lang.toUpperCase();
        } else if (element.dataset.turn) {
          delete element.dataset.turn;
        }
        previous = direction;
        previousLang = lang;
      });
    }

    function window_() {
      if (!limit) { return; }
      var cutoff = order.length - limit;
      order.forEach(function (id, index) {
        nodes[id].element.hidden = index < cutoff;
      });
    }

    function paint(entry) {
      var text = entry.pieces.slice(0, entry.shown).join('');
      if (entry.element.textContent !== text) {
        entry.element.textContent = text;
        onPaint();
      }
    }

    function newest() {
      return order.length ? nodes[order[order.length - 1]] : null;
    }

    function finishAllButNewest() {
      /* Only the last paragraph animates. Once a block is finished the words
       * behind it are history and should not be dribbling in. */
      for (var i = 0; i < order.length - 1; i += 1) {
        var entry = nodes[order[i]];
        if (entry.shown !== entry.pieces.length) {
          entry.shown = entry.pieces.length;
          paint(entry);
        }
      }
    }

    function pendingCount() {
      var entry = newest();
      if (!entry) { return 0; }
      return entry.pieces.length - entry.shown;
    }

    function step() {
      ticking = null;
      var entry = newest();
      if (!entry) { onIdle(true); return; }

      var pending = entry.pieces.length - entry.shown;
      if (pending <= 0) {
        pendingSince = 0;
        onIdle(true);
        return;
      }

      var now = Date.now();
      if (!pendingSince) { pendingSince = now; }
      onIdle(false);

      if (now - pendingSince >= MAX_LAG_MS) {
        // Too far behind to keep pacing: show it all and start again clean.
        entry.shown = entry.pieces.length;
        pendingSince = 0;
        paint(entry);
        /* Must still come back round. Returning here instead left the loop
         * stopped with `settled` false, so `onIdle(true)` never fired and
         * the listening dots stayed down for the rest of the session - the
         * exact "looks stuck" this view exists to avoid. It bites whenever a
         * chunk is too big to pace inside MAX_LAG_MS, which includes the
         * first snapshot a page receives. */
        schedule();
        return;
      }

      // Release a word only when its share of the window has elapsed. The
      // whole backlog is budgeted, so more pending text reveals faster.
      var perWord = Math.max(MIN_STEP_MS,
                             Math.min(MAX_STEP_MS, REVEAL_WINDOW_MS / pending));
      if (now - lastStepAt >= perWord) {
        lastStepAt = now;
        entry.shown += 1;
        // A separator is not a word; take it with the word it belongs to.
        while (entry.shown < entry.pieces.length
               && /^\s+$/.test(entry.pieces[entry.shown])) {
          entry.shown += 1;
        }
        if (entry.shown >= entry.pieces.length) { pendingSince = 0; }
        paint(entry);
      }
      schedule();
    }

    function schedule() {
      if (ticking !== null) { return; }
      ticking = window.requestAnimationFrame(step);
    }

    return {
      upsert: function (para) {
        var entry = node(para);
        dress(entry.element, para);
        var full = (para.final || '') + (para.partial || '');
        if (full === entry.full) { return; }

        var grew = full.indexOf(entry.full) === 0;
        entry.full = full;
        entry.pieces = words(full);
        if (!grew) {
          /* A retroactive sentence cut moved text into the next paragraph,
           * so this one got *shorter*. Nothing to animate - just settle. */
          entry.shown = entry.pieces.length;
          paint(entry);
        } else if (entry.shown > entry.pieces.length) {
          entry.shown = entry.pieces.length;
        }
        finishAllButNewest();
        markDirectionChanges();
        window_();
        schedule();
      },

      retain: function (present) {
        order.slice().forEach(function (id) {
          if (present[id]) { return; }
          remove(id);
        });
        markDirectionChanges();
        window_();
      },

      /* True while there is nothing left to reveal - what the listening
       * indicator keys off. */
      settled: function () { return pendingCount() === 0; },
      count: function () { return order.length; },
      /* Tests read this rather than racing the animation. */
      finishNow: function () {
        order.forEach(function (id) {
          var entry = nodes[id];
          entry.shown = entry.pieces.length;
          paint(entry);
        });
        pendingSince = 0;
        onIdle(true);
      }
    };
  }

  window.CaptionAuto = {
    create: createAutoView,
    REVEAL_WINDOW_MS: REVEAL_WINDOW_MS,
    MAX_LAG_MS: MAX_LAG_MS
  };
}());
