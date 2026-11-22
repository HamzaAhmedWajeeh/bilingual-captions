/* Operator page.
 *
 * Written to be read during a talk, not studied afterwards: the sticky top
 * bar carries the state, the clock, the viewer count and the two buttons,
 * and everything below it is reference.
 *
 * The admin token is read from the URL and sent back on every request; the
 * server re-checks it each time, so this page holds no authority of its own
 * and nothing here is trusted. It is deliberately not stored in
 * localStorage: the page is opened once from a bookmark on the venue laptop,
 * and a token in web storage outlives the talk.
 */

(function () {
  'use strict';

  var POLL_MS = 1000;
  /* The meter polls faster than the rest. A level that updates once a second
   * cannot show a syllable, and showing the gain is the whole point of it. */
  var METER_MS = 150;
  /* Meter range, matching the floor the server reports. The green band and
   * the clip zone in the stylesheet are placed against this same scale. */
  var FLOOR_DB = -60;

  var token = new URLSearchParams(window.location.search).get('token') || '';

  var els = {
    source: document.getElementById('source'),
    screenView: document.getElementById('screen-view'),
    screenSize: document.getElementById('screen-size'),
    screenLogo: document.getElementById('screen-logo'),
    screenGlow: document.getElementById('screen-glow'),
    screenFooter: document.getElementById('screen-footer'),
    screenLabels: document.getElementById('screen-labels'),
    screenCredit: document.getElementById('screen-credit'),
    screenNote: document.getElementById('screen-note'),
    screenHealth: document.getElementById('screen-health'),
    mark: document.getElementById('mark'),
    markNote: document.getElementById('mark-note'),
    markSent: document.getElementById('mark-note-sent'),
    diagnosticsList: document.getElementById('diagnostics-list'),
    diagnosticsEmpty: document.getElementById('diagnostics-empty'),
    diagnosticsSize: document.getElementById('diagnostics-size'),
    screenReload: document.getElementById('screen-reload'),
    screenReloadNote: document.getElementById('screen-reload-note'),
    screenEyebrow: document.getElementById('screen-eyebrow'),
    screenHeadline: document.getElementById('screen-headline'),
    screenHighlight: document.getElementById('screen-highlight'),
    screenSubline: document.getElementById('screen-subline'),
    screenPill: document.getElementById('screen-pill'),
    screenCta: document.getElementById('screen-cta'),
    /* The small line under the card. `screenNote` above is the sent
     * flash on the Main screen row - different thing, similar name. */
    screenNoteLine: document.getElementById('screen-noteline'),
    screenInvite: document.getElementById('screen-invite'),
    screenInviteRow: document.getElementById('screen-invite-row'),
    screenQrSource: document.getElementById('screen-qr-source'),
    screenQrNote: document.getElementById('screen-qr-note'),
    capture: document.getElementById('capture'),
    file: document.getElementById('file'),
    device: document.getElementById('device'),
    channel: document.getElementById('channel'),
    refreshDevices: document.getElementById('refresh-devices'),
    deviceNote: document.getElementById('device-note'),
    deviceNoteRow: document.getElementById('device-note-row'),
    engineHealth: document.getElementById('engine-health'),
    engineHealthRow: document.getElementById('engine-health-row'),
    record: document.getElementById('record'),
    logResponses: document.getElementById('log-responses'),
    transcript: document.getElementById('transcript'),
    transcriptTitle: document.getElementById('transcript-title'),
    transcriptList: document.getElementById('transcript-list'),
    transcriptEmpty: document.getElementById('transcript-empty'),
    transcriptSource: document.getElementById('transcript-source'),
    transcriptBuild: document.getElementById('transcript-build'),
    transcriptState: document.getElementById('transcript-state'),
    transcriptRow: document.getElementById('s-transcript'),
    arm: document.getElementById('arm'),
    paced: document.getElementById('paced'),
    start: document.getElementById('start'),
    stop: document.getElementById('stop'),
    hint: document.getElementById('hint'),
    confirmBar: document.getElementById('confirm-bar'),
    confirmYes: document.getElementById('confirm-yes'),
    confirmNo: document.getElementById('confirm-no'),
    pill: document.getElementById('state-pill'),
    tElapsed: document.getElementById('t-elapsed'),
    tViewers: document.getElementById('t-viewers'),
    tEngine: document.getElementById('t-engine'),
    tSpend: document.getElementById('t-spend'),
    tRecording: document.getElementById('t-recording'),
    meterFill: document.getElementById('meter-fill'),
    meterNote: document.getElementById('meter-note'),
    level: document.getElementById('s-level'),
    clip: document.getElementById('s-clip'),
    sourceText: document.getElementById('s-source'),
    elapsed: document.getElementById('s-elapsed'),
    audio: document.getElementById('s-audio'),
    responses: document.getElementById('s-responses'),
    seq: document.getElementById('s-seq'),
    viewers: document.getElementById('s-viewers'),
    shed: document.getElementById('s-shed'),
    audioDropped: document.getElementById('s-audio-dropped'),
    recording: document.getElementById('s-recording'),
    responseLog: document.getElementById('s-capture'),
    engine: document.getElementById('s-engine'),
    model: document.getElementById('s-model'),
    modelNote: document.getElementById('model-note'),
    lag: document.getElementById('s-lag'),
    reconnects: document.getElementById('s-reconnects'),
    rollovers: document.getElementById('s-rollovers'),
    lost: document.getElementById('s-lost'),
    silence: document.getElementById('s-silence'),
    thisRun: document.getElementById('s-this-run'),
    error: document.getElementById('s-error'),
    errorBanner: document.getElementById('error-banner'),
    errorText: document.getElementById('error-text'),
    errorAdvice: document.getElementById('error-advice'),
    pauseBanner: document.getElementById('pause-banner'),
    pauseText: document.getElementById('pause-text'),
    resume: document.getElementById('resume'),
    resumeNote: document.getElementById('resume-note'),
    statusHeading: document.getElementById('status-heading'),
    statusNote: document.getElementById('status-note'),
    statusCard: document.getElementById('status-card'),
    spend: document.getElementById('s-spend'),
    cost: document.getElementById('s-cost'),
    left: document.getElementById('s-left'),
    spendFill: document.getElementById('spend-fill'),
    spendBar: document.getElementById('spend-bar'),
    spendNote: document.getElementById('spend-note'),
    spendCard: document.getElementById('spend-card'),
    free: document.getElementById('s-free'),
    runs: document.getElementById('s-runs'),
    page: document.getElementById('page-problem'),
    sourceCard: document.getElementById('source-card'),
    detailsCard: document.getElementById('details-card'),
    detailsNote: document.getElementById('details-note'),
    applyBar: document.getElementById('apply-bar'),
    applyNote: document.getElementById('apply-note'),
    dTitle: document.getElementById('d-title'),
    dSpeakers: document.getElementById('d-speakers'),
    dCompanies: document.getElementById('d-companies'),
    dTerms: document.getElementById('d-terms'),
    dNotes: document.getElementById('d-notes'),
    dGlossary: document.getElementById('d-glossary'),
    dSave: document.getElementById('d-save'),
    dApply: document.getElementById('d-apply'),
    dClear: document.getElementById('d-clear'),
    dState: document.getElementById('d-state'),
    details: document.getElementById('s-details')
  };

  /* One short line each, in the words an operator would use.
   *
   * "The screen", not "the phones": the audience does not read captions on
   * their own devices any more - the design is the main screen only, so
   * the only thing watching is the projector. The viewer count still counts
   * sockets, because that is still what it is. */
  var HINTS = {
    replay: 'Plays a recording you already have. Free, and the safest way to ' +
            'check the screen.',
    mic: 'Shows the microphone level. No captions, and nothing is charged.',
    file: 'Plays an audio file through the microphone path. No captions, ' +
          'nothing charged.',
    live: 'The real thing. Captions reach the screen and paid minutes are ' +
          'used. Ten quiet minutes stop it on their own.'
  };

  function url(path, extra) {
    var query = new URLSearchParams(extra || {});
    query.set('token', token);
    return path + '?' + query.toString();
  }

  function mode() { return els.source.value; }

  function showRows() {
    var current = mode();
    var rows = document.querySelectorAll('[data-for]');
    Array.prototype.forEach.call(rows, function (row) {
      var wanted = row.getAttribute('data-for').split(' ');
      row.hidden = wanted.indexOf(current) === -1;
    });
    els.hint.textContent = HINTS[current] || '';
    /* The live start is red, every other start is blue. The colour is the
     * first signal that this button is not like the others. */
    var live = current === 'live';
    els.start.className = live ? 'spend' : 'primary';
    els.start.textContent = live ? 'Start live' : 'Start';
    els.sourceCard.className = live ? 'card danger' : 'card';
    hideConfirm();
  }

  /* Only rebuild a dropdown when its contents actually changed, or the
   * operator's selection would reset itself once a second. */
  function fillSelect(select, values, labels) {
    /* Defensive about both arguments. The operator page is the thing you
     * reach for when something has already gone wrong, so it has to survive
     * a payload or a DOM that is not what it expected. */
    if (!select || !select.options) { return; }
    values = values || [];
    var current = Array.prototype.map.call(select.options, function (o) {
      return o.value;
    });
    if (current.join('\u0000') === values.join('\u0000')) { return; }
    var chosen = select.value;
    select.textContent = '';
    values.forEach(function (value, index) {
      var option = document.createElement('option');
      option.value = value;
      option.textContent = labels ? labels[index] : value;
      select.appendChild(option);
    });
    if (values.indexOf(chosen) !== -1) { select.value = chosen; }
  }

  /* Write one field. Missing values become a hyphen rather than "undefined"
   * or an exception - a status page is most useful exactly when the thing it
   * reports is broken, so no single unexpected field may blank the rest. */
  function text(el, value, fallback) {
    if (!el) { return; }
    el.textContent = (value === null || value === undefined || value === '')
      ? (fallback === undefined ? '-' : fallback)
      : value;
  }

  /* "a / b", or a hyphen unless both halves are really there. */
  function pair(a, b) {
    var ok = function (v) { return v !== null && v !== undefined; };
    return ok(a) && ok(b) ? a + ' / ' + b : '-';
  }

  /* "1 speaker", "2 speakers". Worth the four lines: a status line that says
   * "1 speakers" reads as a page nobody checked. */
  function plural(count, one, many) {
    return count + ' ' + (count === 1 ? one : (many || one + 's'));
  }

  function num(value, digits, suffix) {
    if (typeof value !== 'number' || !isFinite(value)) { return '-'; }
    return value.toFixed(digits) + (suffix || '');
  }

  function clock(secs) {
    if (typeof secs !== 'number' || !isFinite(secs)) { return '-'; }
    var whole = Math.floor(secs);
    var mins = Math.floor(whole / 60);
    var rest = whole % 60;
    return mins + ':' + (rest < 10 ? '0' : '') + rest;
  }

  /* Run one part of the render in isolation. One bad field used to take the
   * whole page down with it: a duplicate key in `els` once made `fillSelect`
   * throw on the first line of render(), and every row showed a hyphen. */
  /* Was the last poll a running session? Used to notice the moment a run
   * ends, which is when its transcript gains its stats. */
  var wasRunning = false;

  function section(name, fn) {
    try {
      fn();
    } catch (error) {
      pageProblem(name + ': ' + (error && error.message || error));
    }
  }

  function renderMeter(level) {
    if (!level) {
      els.meterFill.style.width = '0%';
      els.level.textContent = '-';
      els.clip.hidden = true;
      return;
    }
    var db = level.db;
    var fraction = Math.max(0, Math.min(1, (db - FLOOR_DB) / -FLOOR_DB));
    els.meterFill.style.width = (fraction * 100).toFixed(1) + '%';
    /* Stretch the gradient back out to the width of the whole track, so its
     * colour stops stay lined up with the marks painted on the bar. Green
     * through the good band, amber past it, red inside the clip zone. */
    els.meterFill.style.backgroundSize =
      (100 / Math.max(fraction, 0.001)).toFixed(2) + '% 100%';
    els.level.textContent = db.toFixed(1) + ' dB  (peak ' +
      level.peak_db.toFixed(1) + ')';
    els.clip.hidden = !level.clipping;
  }

  /* -- the state pill ---------------------------------------------------- */

  function pillFor(status) {
    if (status.engine_state === 'paused') { return ['PAUSED', 'paused']; }
    if (!status.running) { return ['STOPPED', 'stopped']; }
    if (status.mode === 'replay') { return ['REPLAY', 'replay']; }
    /* A mic or file run captures audio but transcribes nothing, so it is
     * neither live nor a replay. Saying so beats implying captions. */
    if (!status.engine_state) { return ['AUDIO', 'audio']; }
    return ['LIVE', 'live'];
  }

  /* "Is the audio being saved?", where it can be read without scrolling.
   *
   * The status table already carried the file's path, which answers the
   * question only if you go looking. On 2026-10-31 two talks locked up and
   * neither could be replayed through the engine, because the box had been
   * cleared by a page load and nobody could tell afterwards. The whole point
   * of this line is to be visible during the talk, while it can still be
   * fixed by a Stop and a Start.
   *
   * It reports what the *session* is doing - the server sends the recorder's
   * path or nothing - not what the checkbox says. Those differ for every
   * source but live, and after a reload they differ until Start is pressed.
   */
  function renderRecording(status) {
    if (!els.tRecording) { return; }
    var saving = !!status.recording;
    if (!status.running) {
      /* Between sessions the checkbox is a plan, not a fact. Say which. */
      els.tRecording.textContent = els.record && els.record.checked
        ? 'will save' : 'not saving';
      els.tRecording.className = '';
      return;
    }
    els.tRecording.textContent = saving ? 'saving' : 'not saved';
    els.tRecording.className = saving ? 'ok' : 'warn';
  }

  function renderSpend(usage) {
    if (!usage) { return; }
    text(els.spend, num(usage.billable_minutes, 1));
    text(els.tSpend, num(usage.billable_minutes, 1, ' min'));

    var used = usage.estimated_usd;
    var budget = usage.budget_usd;
    if (used === null || used === undefined) {
      /* No rate configured, so no honest dollar figure. Saying so is better
       * than printing a number that looks measured. */
      text(els.cost, null);
      text(els.left, null);
      els.spendFill.style.width = '0%';
      els.spendBar.hidden = true;
      text(els.spendNote,
        'Set the per-minute rate in .env to see dollars.');
    } else {
      text(els.cost, '$' + used.toFixed(2));
      els.spendBar.hidden = false;
      if (budget) {
        var left = budget - used;
        text(els.left, '$' + left.toFixed(2));
        var share = Math.max(0, Math.min(1, used / budget));
        els.spendFill.style.width = (share * 100).toFixed(1) + '%';
        els.spendFill.className = share > 0.9 ? 'out'
          : (share > 0.7 ? 'low' : '');
        text(els.spendNote, 'Of a $' + budget.toFixed(2) + ' budget.', '');
        els.spendCard.className = share > 0.9 ? 'card danger' : 'card';
      } else {
        text(els.left, null);
        text(els.spendNote, '', '');
      }
    }
    text(els.free, num(usage.free_minutes, 1, ' min'));
    text(els.runs, usage.runs === undefined ? null : usage.runs +
      (usage.unreadable_lines ? ' (' + usage.unreadable_lines +
        ' unreadable)' : ''));
  }

  /* Rows whose value is only a hyphen are noise while nothing is running.
   * Hidden rather than removed so they come straight back when a run starts.
   *
   * The Connection group goes as a block when there is no engine at all: a
   * replay transcribes nothing, so "caption delay" and "reconnects" are not
   * blank, they are meaningless. */
  function tidyRows(running, hasEngine) {
    var rows = els.statusCard.querySelectorAll('tr[data-row], tr.group');
    Array.prototype.forEach.call(rows, function (row) {
      if (row.getAttribute('data-group') === 'connection' && !hasEngine) {
        row.hidden = true;
        return;
      }
      if (!row.hasAttribute('data-row')) { row.hidden = false; return; }
      var cell = row.cells[1];
      var empty = !cell || cell.textContent === '-';
      row.hidden = empty && !running;
    });
  }

  function render(status) {
    status = status || {};
    var running = !!status.running;

    section('sources', function () {
      fillSelect(els.capture, status.captures);
      fillSelect(els.file, status.audio_files);
      /* Only the live logs. A fixture or a saved capture was something
       * replayed, and a replay has no transcript to make - the server
       * refuses one either way, so offering it would only be a dead end. */
      fillSelect(els.transcriptSource, (status.captures || []).filter(
        function (name) { return name.indexOf('live-') === 0; }));
    });

    section('transcripts', function () {
      /* A run that has just ended has written its stats, so the list is
       * worth re-reading once - and only once. */
      if (!running && wasRunning) { loadTranscripts(); }
      wasRunning = running;
    });

    section('topbar', function () {
      var state = pillFor(status);
      els.pill.textContent = state[0];
      els.pill.className = 'pill ' + state[1];
      text(els.tElapsed, running ? clock(status.elapsed) : null);
      text(els.tViewers, status.viewers);
      text(els.tEngine, status.engine_state, running ? 'running' : 'idle');
      els.start.disabled = running;
      els.stop.disabled = !running;
    });

    section('status', function () {
      /* Leftover numbers from a finished run are useful, but only if they
       * are labelled as the past. Showing them unmarked is how an operator
       * reads a stopped session as a live one. */
      var stale = !running && status.source;
      text(els.statusHeading, stale ? 'Last run' : 'Status');
      els.statusHeading.className = stale ? 'stale' : '';
      /* The empty fallback matters: `text` turns '' into a hyphen by
       * default, which left a stray dash hanging under the table. */
      text(els.statusNote, stale
        ? 'These are from the run that just finished, not from now.'
        : '', '');

      text(els.sourceText, status.source);
      text(els.elapsed, status.elapsed === undefined || status.elapsed === null
        ? null : status.elapsed + ' s');
      text(els.audio, status.audio_total_s
        ? num(status.audio_ms / 1000, 1) + ' / ' + status.audio_total_s + ' s'
        : (status.audio_ms ? num(status.audio_ms / 1000, 1, ' s') : null));
      text(els.responses, status.responses);
      text(els.seq, status.seq);
      text(els.viewers, pair(status.viewers, status.max_viewers));
      text(els.shed, pair(status.rejected, status.dropped));
      text(els.audioDropped, status.dropped_chunks === undefined
        || status.dropped_chunks === null
        ? null : status.dropped_chunks);
      text(els.recording, status.recording, 'off');
      renderRecording(status);
      text(els.responseLog, status.capture, 'off');
      text(els.transcriptRow, status.transcript, 'off');

      /* Counts only. The values are never in this payload. */
      var d = status.details;
      text(els.details, d ? [
        d.title ? 'title' : 'no title',
        plural(d.speakers, 'speaker'),
        plural(d.companies, 'company', 'companies'),
        plural(d.terms, 'word')
      ].join(', ') : null);

      text(els.engine, status.engine_state);
      els.engine.className = status.engine_state === 'reconnecting'
        ? 'error' : '';
      /* A running session overwrites `model` with the one its own connection
       * asked for, so this row never reports a settings file edited while
       * the talk was in progress. */
      text(els.model, status.model);
      text(els.modelNote, status.model
        ? 'Speech model: ' + status.model + '.' : '', '');
      text(els.lag, num(status.lag_seconds, 2, ' s'));
      text(els.reconnects, status.reconnects);
      text(els.rollovers, status.rollovers);
      text(els.lost, num(status.dropped_seconds, 1, ' s'));
      els.lost.className = status.dropped_seconds ? 'error' : '';
      text(els.silence, num(status.silent_seconds, 0, ' s'));
      text(els.thisRun, num(
        typeof status.streamed_seconds === 'number'
          ? status.streamed_seconds / 60 : undefined, 2, ' min'));

      /* An engine is involved when one is connected now, or when the run
       * that just finished had one. A replay never has one. */
      tidyRows(running, !!(status.engine_state
        || status.reconnects !== undefined || status.dropped_seconds));
    });

    section('error', function () {
      /* The engine's error, not the page's. A JavaScript exception used to
       * land here, which read as "the transcription failed" when the truth
       * was "this page has a bug". Page problems go to their own banner. */
      text(els.error, status.error, 'none');
      els.error.className = status.error ? 'error' : '';
      text(els.errorText, status.error, '');
      els.errorBanner.hidden = !status.error;
      /* One plain line for the error an operator can actually act on. The
       * key is the only thing on this list they can fix from their seat. */
      var auth = /unauthenticated|permission_denied|invalid api key/i
        .test(status.error || '');
      text(els.errorAdvice, auth ? 'The API key in .env was rejected.' : '', '');
      els.errorAdvice.hidden = !auth;
      els.statusCard.className = status.error ? 'card danger' : 'card';
    });

    section('pause', function () { renderPause(status); });
    section('details', function () { showPending(status.details_pending); });
    section('spend', function () { renderSpend(status.usage); });
    section('screen', function () {
      renderScreen(status.screen);
      renderQrSource(status.qr);
      renderScreenHealth(status.screens);
      renderEngineHealth(status);
    });
    section('diagnostics', function () {
      renderDiagnostics(status.sessions, status.logs_bytes);
    });
  }

  /* The main-screen control.
   *
   * The selects are only written when the operator is not using them: this
   * runs on every status poll, and a poll landing mid-choice would snap the
   * dropdown back under their finger. */
  function renderScreen(screen) {
    if (!screen || !els.screenView) { return; }
    if (document.activeElement !== els.screenView) {
      els.screenView.value = screen.view;
    }
    if (document.activeElement !== els.screenSize) {
      els.screenSize.value = screen.size;
    }
    [[els.screenLogo, 'logo'], [els.screenGlow, 'glow'],
     [els.screenFooter, 'footer'],
     [els.screenLabels, 'labels'], [els.screenHighlight, 'highlight']]
      .forEach(function (pair) {
        if (document.activeElement !== pair[0]) {
          pair[0].checked = !!screen[pair[1]];
        }
      });
    /* The credit is the text itself - typing something is what turns it on -
     * so there is no checkbox to keep in step, only a field not to overwrite
     * while the operator is in it. */
    if (document.activeElement !== els.screenCredit) {
      els.screenCredit.value = screen.credit || '';
    }

    /* The waiting screen's words, on the same rule: write them unless the
     * operator is in the field. `invite` is here and nowhere else - this
     * poll is behind the admin token, and it is the only place in the
     * project that hands the link back as text. */
    [[els.screenEyebrow, 'eyebrow'], [els.screenHeadline, 'headline'],
     [els.screenSubline, 'subline'], [els.screenPill, 'pill'],
     [els.screenCta, 'cta'],
     [els.screenNoteLine, 'note'], [els.screenInvite, 'invite']]
      .forEach(function (pair) {
        if (document.activeElement !== pair[0]) {
          pair[0].value = screen[pair[1]] || '';
        }
      });
  }

  /* What the screens say they are actually showing.
   *
   * **The line that was missing on 2026-10-24.** A projector was reported
   * showing the English original mid-talk; nothing could confirm it, nothing
   * could deny it, and a day went into replaying logs. Every `/screen` now
   * says every five seconds which pane it has visible, whether a URL pinned
   * it there, and how far behind it is - and this turns red when any of them
   * is not showing what the operator chose.
   *
   * "1 screen · Auto · up to date" is the good case, and it is deliberately
   * the same shape of sentence as the bad one so the difference is the
   * words rather than the layout. No caption text: a count, a view name and
   * a number of seconds.
   */
  var VIEW_NAMES = {auto: 'Auto', en: 'English', ar: 'العربية',
                    both: 'Both', none: 'nothing', mixed: 'several panes'};

  function renderScreenHealth(screens) {
    if (!els.screenHealth) { return; }
    if (!screens || !screens.count) {
      els.screenHealth.textContent = 'no screens connected';
      els.screenHealth.className = 'health';
      return;
    }
    var audience = screens.audience || 0;
    var projectors = screens.count - audience;
    var plural = projectors === 1 ? 'screen' : 'screens';
    var quiet = screens.count - screens.reporting;
    var parts = [projectors + ' ' + plural];
    if (audience) {
      /* Listed, not warned about: the audience page follows its own reader
       * by design. Worth seeing all the same - it is the page that renders
       * the English original with grey partials, and nobody is supposed to
       * be on it during a session. */
      parts.push(audience + (audience === 1 ? ' other page' : ' other pages'));
    }
    var views = {};
    (screens.rows || []).forEach(function (row) {
      if (!row.reporting || row.page === 'audience') { return; }
      var name = VIEW_NAMES[row.rendered] || row.rendered;
      if (row.pinned) { name += ' (pinned by URL)'; }
      views[name] = (views[name] || 0) + 1;
    });
    var names = Object.keys(views);
    if (names.length) {
      parts.push(names.map(function (name) {
        return views[name] > 1 ? views[name] + '× ' + name : name;
      }).join(', '));
    }

    var trouble = [];
    /* First, because it is the only one whose remedy is not in this app:
     * the browser is translating the captions back into the language the
     * room just heard, and nothing the operator does here will stop it. */
    if (screens.translated) {
      trouble.push('Browser is translating this screen, turn off '
                   + 'auto-translate');
    }
    /* The check that catches the 2026-10-24 symptom whatever caused it:
     * what the page says is on screen against what the server last put on
     * the end of the auto stream. */
    if (screens.wrong_language) {
      trouble.push('Screen is showing the wrong language');
    }
    if (screens.wrong_view) {
      trouble.push(screens.wrong_view + ' not showing '
                   + (VIEW_NAMES[screens.want] || screens.want));
    }
    if (screens.behind) {
      var worst = 0;
      (screens.rows || []).forEach(function (row) {
        if (row.reporting && row.behind_ms > worst) { worst = row.behind_ms; }
      });
      trouble.push(screens.behind + ' behind by '
                   + Math.round(worst / 1000) + 's');
    }
    /* Non-final text in the translation-only pane. It should be impossible,
     * and it is precisely what the 2026-10-24 report described, so it gets
     * its own words rather than being folded into "not showing Auto". */
    if (screens.grey) {
      trouble.push(screens.grey + ' showing unconfirmed text in Auto');
    }
    if (screens.stale) { trouble.push(screens.stale + ' not reporting'); }
    if (quiet > 0 && !screens.stale) {
      parts.push(quiet + ' not reporting yet');
    }

    if (trouble.length) {
      els.screenHealth.textContent = parts.join(' · ') + ': '
        + trouble.join(', ');
      els.screenHealth.className = 'health bad';
    } else {
      els.screenHealth.textContent = parts.join(' · ') + ' · up to date';
      els.screenHealth.className = 'health ok';
    }
  }

  /* "Auto-reconnected, language stuck (hh:mm)", and "the input is quiet".
   *
   * The screens line above answers "is the room reading the language the
   * server sent". This answers the question nobody could ask on 2026-10-31,
   * when the answer to that one was yes: *was what the server sent the
   * language anybody spoke*. The engine stopped labelling speech English 18
   * minutes in, an English speaker was transcribed in Arabic letters, and
   * the screen faithfully showed the English translation of it - the room
   * read the words it had just heard. Nothing was red, because nothing was
   * wrong between the server and the page.
   *
   * Shares and counts, never a word of what was said: the share is the
   * fraction of a rolling window of Arabic words that are English function
   * words, which is the shape of the text rather than the text.
   */
  function clock(seconds) {
    var whole = Math.max(0, Math.round(seconds));
    var mins = Math.floor(whole / 60);
    var secs = whole % 60;
    return mins + ':' + (secs < 10 ? '0' : '') + secs;
  }

  /* "Paused: no sound from <device> for N s. Check the mic or mixer."
   *
   * The one thing 2026-11-07 did not have. The pill said PAUSED, which is
   * true and says nothing an operator can act on: not what it is waiting
   * for, not how long, not which input, and not that the session is still
   * running underneath it. The pause ends the engine stream and nothing
   * else, so the sentence has to make that plain or Stop + Start is the
   * obvious next move - and it was the one that got made.
   */
  function renderPause(status) {
    if (!els.pauseBanner) { return; }
    var paused = status.engine_state === 'paused';
    els.pauseBanner.hidden = !paused;
    if (!paused) {
      els.resumeNote.textContent = '';
      return;
    }
    var note = status.pause || {};
    var device = note.device || status.source || 'the input';
    var secs = Math.round(note.silent_seconds
      || note.idle_stop_seconds || status.silent_seconds || 0);
    var line = 'Paused: no sound from ' + device + ' for ' + secs
      + ' s. Check the mic or mixer.';
    /* The level it is judging, because "no sound" is a threshold and not a
     * fact - on 2026-11-07 the line was carrying speech the whole time.
     *
     * **"to resume" is load-bearing, and so is where the number comes
     * from.** `input_db` is the engine's own figure and floors at -100 dB;
     * the meter below floors at -60, because that is where a bar stops
     * being worth painting. Read off the meter this line said "Input -60.0
     * dB, needs -66.0 dB" - a level that looks *above* the threshold beside
     * a pause still waiting for it. The sentence now says what the second
     * number is for, so the pair reads as a condition rather than as a
     * contradiction. */
    if (typeof note.input_db === 'number'
        && typeof note.silence_db === 'number') {
      line += ' Input ' + note.input_db.toFixed(1) + ' dB, needs '
        + note.silence_db.toFixed(1) + ' dB to resume.';
    }
    els.pauseText.textContent = line;
  }

  function renderEngineHealth(status) {
    if (!els.engineHealth || !els.engineHealthRow) { return; }
    var notes = [];
    var bad = false;
    var lang = status.lang;
    if (lang) {
      if (lang.capped) {
        /* Six reconnects in an hour that did not fix it. The watcher has
         * stopped asking, and the operator is the one who should decide
         * what to try next - restarting the engine for the rest of a talk
         * is not a remedy, it is a loop. */
        notes.push('Language stuck and reconnecting did not fix it. '
                   + lang.reconnects + ' tries, stopped trying.');
        bad = true;
      } else if (lang.reconnected) {
        notes.push('Auto-reconnected, language stuck (' + clock(lang.at) + ')'
                   + (lang.reconnects > 1
                      ? ', ' + lang.reconnects + ' this hour' : ''));
        bad = true;
      } else {
        notes.push('Language stuck at ' + clock(lang.at)
                   + ', could not reconnect');
        bad = true;
      }
    } else if (status.lang_drifted) {
      /* Logged, not acted on: on 2026-10-31 this held for 17 minutes of a
       * session that recovered on its own twice. Worth a grey line so the
       * operator can watch it, never a reconnect. */
      notes.push('English terms stopped appearing in Latin script');
    }
    if (status.quiet) {
      /* Two different sentences, because the dB figure alone has never been
       * able to tell them apart. On 2026-11-07 a working mixer line sat
       * below the floor for 667 s while 5,655 final tokens arrived through
       * it, and the page spent all of it asking whether the device was
       * wrong. A line the engine is transcribing is the right device at any
       * level, so that case is grey and informational; a quiet line with no
       * words coming out of it is the warning this check was built for. */
      var quietLine = 'Input is quiet, ' + status.quiet.peak_db
        + ' dB peak for ' + Math.round(status.quiet.seconds) + 's';
      if (status.quiet.speech) {
        notes.push(quietLine + ', but it is being transcribed. '
                   + 'Turn ' + status.quiet.device + ' up if you can.');
      } else {
        notes.push(quietLine + '. Is ' + status.quiet.device
                   + ' the right input?');
        bad = true;
      }
    }
    if (!notes.length) {
      els.engineHealthRow.hidden = true;
      return;
    }
    els.engineHealthRow.hidden = false;
    els.engineHealth.textContent = notes.join(' · ');
    els.engineHealth.className = bad ? 'health bad' : 'health';
  }

  /* One row per session, newest first, each with its zip.
   *
   * Names and sizes only - what is in these files goes out through the
   * download and nowhere else. Nothing is ever deleted here: the session
   * worth reading back is the one that went wrong, and it must still be
   * there a week later. The total is shown so the operator can decide when
   * to clean up by hand.
   */
  function renderDiagnostics(sessions, total) {
    if (!els.diagnosticsList) { return; }
    var rows = sessions || [];
    els.diagnosticsList.textContent = '';
    rows.forEach(function (row) {
      /* The transcripts card's shape, and its stylesheet: a head line with
       * the name and what is in it, and a line of links under it. The gap
       * between a value and a link is the one thing that cannot be left to
       * a text node here - `transcript-links` is a flex row. */
      var box = document.createElement('div');
      box.className = 'transcript';

      var head = document.createElement('p');
      head.className = 'transcript-head';
      var name = document.createElement('b');
      name.textContent = row.session;
      head.appendChild(name);
      var meta = document.createElement('span');
      meta.className = 'note';
      meta.textContent = ' ' + bytes(row.bytes) + ' · '
        + (row.files || []).join(', ');
      head.appendChild(meta);
      box.appendChild(head);

      var links = document.createElement('p');
      links.className = 'transcript-links';
      var link = document.createElement('a');
      link.href = url('/admin/diagnostics', {name: row.session});
      link.textContent = 'Download diagnostics';
      link.className = 'strong';
      link.setAttribute('download', '');
      links.appendChild(link);
      box.appendChild(links);

      els.diagnosticsList.appendChild(box);
    });
    els.diagnosticsEmpty.hidden = rows.length > 0;
    els.diagnosticsSize.textContent = total
      ? 'logs/ is holding ' + bytes(total)
        + '. Nothing here is deleted automatically.'
      : '';
  }

  /* Which of the three QR sources is live.
   *
   * Worth its own line on the page: a card quietly showing the fallback
   * because a file landed in the wrong directory looks exactly like one
   * that is working, and the operator is the only person who can see the
   * difference. When the organizer's own file is in use the link field is
   * disabled rather than hidden - it is still saved, and it comes back the
   * moment the file is taken away. */
  function renderQrSource(qr) {
    if (!qr || !els.screenQrSource) { return; }
    els.screenQrSource.textContent = qr.label || '-';
    var fromFile = qr.kind === 'file';
    els.screenInvite.disabled = fromFile;
    els.screenInviteRow.classList.toggle('disabled', fromFile);
    els.screenQrNote.textContent = fromFile
      ? 'The file wins, so the link below is not in use. Remove it to go '
        + 'back to a generated code.'
      : '';
  }

  function pushScreen() {
    els.screenNote.textContent = 'sent';
    window.setTimeout(function () { els.screenNote.textContent = ''; }, 1200);
    post('/admin/screen', {
      view: els.screenView.value,
      size: els.screenSize.value,
      logo: els.screenLogo.checked,
      glow: els.screenGlow.checked,
      footer: els.screenFooter.checked,
      labels: els.screenLabels.checked,
      credit: els.screenCredit.value,
      eyebrow: els.screenEyebrow.value,
      headline: els.screenHeadline.value,
      highlight: els.screenHighlight.checked,
      subline: els.screenSubline.value,
      pill: els.screenPill.value,
      cta: els.screenCta.value,
      note: els.screenNoteLine.value,
      invite: els.screenInvite.value
    });
  }

  /* A problem reaching or reading the server: this page's problem, kept
   * apart from the engine's error so the two are never confused. */
  function pageProblem(message) {
    if (!els.page) { return; }
    els.page.textContent = message || '';
    els.page.hidden = !message;
  }

  function fail(message) {
    pageProblem(message);
  }

  function get(path) {
    return window.fetch(url(path), { cache: 'no-store' })
      .then(function (response) {
        if (!response.ok) { throw new Error('status ' + response.status); }
        return response.json();
      });
  }

  function poll() {
    get('/admin/status')
      .then(function (status) { pageProblem(''); render(status); })
      .catch(function (error) { fail(String(error.message || error)); });
  }

  /* Its own endpoint, not /admin/status: the meter needs several updates a
   * second, and status reads the usage ledger off disk every call. */
  function pollMeter() {
    get('/admin/level')
      .then(function (data) { renderMeter(data.level); })
      .catch(function () { renderMeter(null); });
  }

  /* The microphone dropdown, and the one thing about it that matters.
   *
   * **Pick the interface by name, never the default entry.** On 2026-10-31
   * the mixer was the Windows default but was not in this list under its own
   * name, so three sessions ran on whatever "default" resolved to and the
   * logs and the operator disagreed about what had been selected for a week.
   * `remembered` is what the last live session actually opened, read back
   * off the device PortAudio resolved rather than off the request - so it
   * preselects the thing that worked, and says so when it is gone.
   */
  function renderDevices(data) {
    var devices = data.devices || [];
    var wanted = data.remembered || '';
    fillSelect(
      els.device,
      devices.map(function (d) { return String(d.index); }),
      devices.map(function (d) {
        return d.index + ': ' + d.name + ' (' + d.api + ', ' +
          d.channels + ' ch' + (d.default ? ', default' : '') + ')' +
          (d.name === wanted ? ' (used last time)' : '');
      })
    );
    if (!devices.length) {
      var option = document.createElement('option');
      option.value = '';
      option.textContent = 'no microphone found';
      els.device.appendChild(option);
    }
    /* Preselect by name. The index is only a hint: PortAudio renumbers when
     * anything is plugged in, so last night's index may be a different
     * device tonight. */
    var match = null;
    devices.forEach(function (d) {
      if (d.name === wanted && match === null) { match = d; }
    });
    if (match) {
      els.device.value = String(match.index);
      if (data.remembered_channel) {
        els.channel.value = String(data.remembered_channel);
      }
    }
    /* **The record tick, restored but never defaulted on.** A fresh install
     * sends false and this leaves the box clear, which is the only
     * acceptable default: the room has not consented to being recorded.
     * What it fixes is the opposite failure - a box that was ticked last
     * week, remembered as ticked, and silently cleared by every page load
     * since. Restored here rather than on its own request because this is
     * already the one fetch that reads the operator's capture choices. */
    if (els.record && data.remembered_record) {
      els.record.checked = true;
    }
    renderRecording({running: false});
    if (els.deviceNote && els.deviceNoteRow) {
      if (data.missing) {
        els.deviceNote.textContent =
          wanted + ' (used last time) is not in the list. Press Refresh '
          + 'devices, or restart the server.';
        els.deviceNote.className = 'note bad';
        els.deviceNoteRow.hidden = false;
      } else if (match) {
        els.deviceNote.textContent =
          'Preselected ' + wanted + ', used '
          + (data.remembered_at || 'last time') + '.';
        els.deviceNote.className = 'note';
        els.deviceNoteRow.hidden = false;
      } else {
        els.deviceNoteRow.hidden = true;
      }
    }
  }

  function loadDevices() {
    get('/admin/devices')
      .then(renderDevices)
      .catch(function (error) { fail(String(error.message || error)); });
  }

  /* Ask the server to re-read the driver, not just to re-send its cache.
   * Refused with 409 while a session is up, and the refusal is the message
   * the operator needs: stopping first is the price of seeing a device that
   * was plugged in late. */
  function refreshDevices() {
    window.fetch(url('/admin/devices/refresh', {}),
                 { method: 'POST', cache: 'no-store' })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            throw new Error(body.detail || response.status);
          }
          renderDevices(body);
        });
      })
      .catch(function (error) {
        if (els.deviceNote && els.deviceNoteRow) {
          els.deviceNote.textContent = String(error.message || error);
          els.deviceNote.className = 'note bad';
          els.deviceNoteRow.hidden = false;
        }
      });
  }

  function post(path, extra) {
    window.fetch(url(path, extra), { method: 'POST', cache: 'no-store' })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) {
            throw new Error(body.detail || ('status ' + response.status));
          }
          return body;
        });
      })
      .then(render)
      .catch(function (error) { fail(String(error.message || error)); });
  }

  /* -- this talk's details ---------------------------------------------
   *
   * Private. They are loaded by their own request and never arrive with the
   * status poll, so a page left open on a side screen shows counts and
   * nothing else. Lists are one item per line, because a name can contain a
   * comma and an operator should not have to think about it.
   */

  function lines(value) {
    return String(value || '').split('\n')
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length; });
  }

  function detailsState(message, kind) {
    if (!els.dState) { return; }
    els.dState.textContent = message || '';
    els.dState.className = 'note' + (kind ? ' ' + kind : '');
  }

  function loadDetails() {
    get('/admin/session')
      .then(function (data) {
        var d = data.details || {};
        els.dTitle.value = d.title || '';
        els.dSpeakers.value = (d.speakers || []).join('\n');
        els.dCompanies.value = (d.companies || []).join('\n');
        els.dTerms.value = (d.terms || []).join('\n');
        els.dNotes.value = d.notes || '';
        els.dGlossary.textContent = 'Always included: '
          + (data.glossary_terms || []).join(', ');
        showPending(data.pending);
      })
      .catch(function (error) { fail(String(error.message || error)); });
  }

  /* Saved-but-not-applied is the one state on this page an operator must not
   * walk past, so it gets a bar of its own and the card border turns amber. */
  function showPending(pending) {
    if (!els.applyBar) { return; }
    els.applyBar.hidden = !pending;
    els.detailsCard.className = pending ? 'card attention' : 'card';
    /* The amber bar says "saved, not in use yet" in full. Saying it again
     * beside the Save button only made the page look like it was repeating
     * itself, so the small line is cleared instead. */
    if (pending) { detailsState('', null); }
  }

  function sendDetails(details, note) {
    return window.fetch(url('/admin/session'), {
      method: 'POST',
      cache: 'no-store',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(details)
    })
      .then(function (response) {
        if (!response.ok) { throw new Error('status ' + response.status); }
        return response.json();
      })
      .then(function (data) {
        detailsState(note, 'saved');
        showPending(data.pending);
        return data;
      })
      .catch(function (error) { fail(String(error.message || error)); });
  }

  els.dSave.addEventListener('click', function () {
    sendDetails({
      title: els.dTitle.value.trim(),
      speakers: lines(els.dSpeakers.value),
      companies: lines(els.dCompanies.value),
      terms: lines(els.dTerms.value),
      notes: els.dNotes.value.trim()
    }, 'Saved.');
  });

  els.dClear.addEventListener('click', function () {
    els.dTitle.value = '';
    els.dSpeakers.value = '';
    els.dCompanies.value = '';
    els.dTerms.value = '';
    els.dNotes.value = '';
    sendDetails({title: '', speakers: [], companies: [], terms: [], notes: ''},
                'Cleared.');
  });

  els.dApply.addEventListener('click', function () {
    window.fetch(url('/admin/session/apply'), {method: 'POST', cache: 'no-store'})
      .then(function (response) {
        if (!response.ok) { throw new Error('status ' + response.status); }
        return response.json();
      })
      .then(function (data) {
        showPending(data.pending);
        detailsState(data.applied
          ? 'In use now.'
          : 'Nothing running, they will be used at the next start.', 'saved');
      })
      .catch(function (error) { fail(String(error.message || error)); });
  });

  /* -- transcripts --------------------------------------------------------
   *
   * Its own request, not the status poll: a list of what has been written
   * down changes a handful of times a session, and the poll runs once a
   * second. Reloaded after a run ends, after a build and after a delete.
   */

  var transcriptSeen = '';

  function transcriptState(message, kind) {
    if (!els.transcriptState) { return; }
    els.transcriptState.textContent = message || '';
    els.transcriptState.className = 'note' + (kind ? ' ' + kind : '');
  }

  function bytes(count) {
    if (!count) { return '0 KB'; }
    if (count < 1024 * 1024) { return Math.round(count / 1024) + ' KB'; }
    return (count / 1024 / 1024).toFixed(1) + ' MB';
  }

  /* One row per session. Built as nodes rather than markup: every value here
   * comes off a JSON payload, and a transcript folder is named after a real
   * session - there is no reason for this page to be able to turn any of it
   * into HTML. */
  function renderTranscripts(list) {
    if (!els.transcriptList) { return; }
    list = list || [];
    var fingerprint = JSON.stringify(list);
    if (fingerprint === transcriptSeen) { return; }
    transcriptSeen = fingerprint;

    els.transcriptList.textContent = '';
    els.transcriptEmpty.hidden = list.length > 0;

    list.forEach(function (row) {
      var box = document.createElement('div');
      box.className = 'transcript';

      var head = document.createElement('p');
      head.className = 'transcript-head';
      var name = document.createElement('b');
      name.textContent = row.name.replace('_', ' ');
      head.appendChild(name);
      var meta = document.createElement('span');
      meta.className = 'note';
      meta.textContent = ' ' + [
        row.duration || 'duration unknown',
        row.words ? row.words + ' words' : null,
        bytes(row.bytes)
      ].filter(Boolean).join(' · ');
      head.appendChild(meta);
      box.appendChild(head);

      var links = document.createElement('p');
      links.className = 'transcript-links';
      (row.files || []).forEach(function (file) {
        var link = document.createElement('a');
        link.href = url('/admin/transcripts/file',
                        {name: row.name, file: file});
        link.textContent = file;
        links.appendChild(link);
      });
      var zip = document.createElement('a');
      zip.href = url('/admin/transcripts/zip', {name: row.name});
      zip.className = 'strong';
      zip.textContent = 'all as .zip';
      links.appendChild(zip);
      box.appendChild(links);

      var remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'ghost';
      remove.textContent = 'Delete';
      /* Two clicks, inline. `confirm()` is a modal dialog, and a modal
       * dialog blocks every later instruction this page receives - Stop
       * included. Same reasoning as the live-run confirm bar. */
      remove.addEventListener('click', function () {
        if (remove.dataset.armed !== '1') {
          remove.dataset.armed = '1';
          remove.textContent = 'Really delete?';
          remove.className = 'ghost warn';
          window.setTimeout(function () {
            if (remove.dataset.armed === '1') {
              remove.dataset.armed = '';
              remove.textContent = 'Delete';
              remove.className = 'ghost';
            }
          }, 4000);
          return;
        }
        remove.disabled = true;
        window.fetch(url('/admin/transcripts/delete', {name: row.name}),
                     {method: 'POST', cache: 'no-store'})
          .then(function (response) {
            if (!response.ok) { throw new Error('status ' + response.status); }
            return response.json();
          })
          .then(function (data) {
            transcriptState('Deleted ' + row.name + '.', 'saved');
            transcriptSeen = '';
            renderTranscripts(data.transcripts);
          })
          .catch(function (error) {
            remove.disabled = false;
            fail(String(error.message || error));
          });
      });
      box.appendChild(remove);

      els.transcriptList.appendChild(box);
    });
  }

  function loadTranscripts() {
    get('/admin/transcripts')
      .then(function (data) { renderTranscripts(data.transcripts); })
      .catch(function (error) { fail(String(error.message || error)); });
  }

  if (els.transcriptBuild) {
    els.transcriptBuild.addEventListener('click', function () {
      var capture = els.transcriptSource.value;
      if (!capture) {
        transcriptState('No earlier live run to work from.', null);
        return;
      }
      els.transcriptBuild.disabled = true;
      transcriptState('Working through ' + capture + '…', null);
      window.fetch(url('/admin/transcripts/build', {capture: capture}),
                   {method: 'POST', cache: 'no-store'})
        .then(function (response) {
          return response.json().then(function (body) {
            if (!response.ok) {
              throw new Error(body.detail || ('status ' + response.status));
            }
            return body;
          });
        })
        .then(function (data) {
          transcriptState('Made ' + data.built + '.', 'saved');
          transcriptSeen = '';
          renderTranscripts(data.transcripts);
        })
        .catch(function (error) { fail(String(error.message || error)); })
        .then(function () { els.transcriptBuild.disabled = false; });
    });
  }

  /* -- starting ---------------------------------------------------------- */

  function hideConfirm() {
    if (els.confirmBar) { els.confirmBar.hidden = true; }
  }

  function startParams() {
    var current = mode();
    var params = {
      source: current,
      paced: els.paced.checked ? 'true' : 'false',
      record: els.record.checked ? 'true' : 'false',
      log_responses: els.logResponses.checked ? 'true' : 'false',
      /* Sent whatever the source is, and ignored by the server for every
       * source but `live` - the enforcement is there, not here. */
      transcript: els.transcript.checked ? 'true' : 'false',
      transcript_title: els.transcriptTitle.checked ? 'true' : 'false'
    };
    if (current === 'replay') {
      if (!els.capture.value) { return null; }
      params.capture = els.capture.value;
    } else if (current === 'file') {
      if (!els.file.value) { return null; }
      params.file = els.file.value;
    } else if (current === 'mic' || current === 'live') {
      if (els.device.value !== '') { params.device = els.device.value; }
      params.channel = els.channel.value || '1';
    }
    return params;
  }

  /* Refresh screens: the mid-talk fix. Every `/screen` reloads itself, the
   * session keeps running and the transcript keeps writing into the same
   * folder - where Stop + Start ends the talk to fix a browser. */
  els.screenReload.addEventListener('click', function () {
    els.screenReloadNote.textContent = 'reloading…';
    window.setTimeout(function () {
      els.screenReloadNote.textContent = '';
    }, 2500);
    post('/admin/screens/reload', {});
  });

  /* Resume: out of the idle pause without a new session. The stream reopens
   * through the rollover path, the ring buffer covers the join and the
   * transcript keeps writing into the same folder - where Stop + Start ends
   * the talk, which is what 2026-11-07 had to do to get out of a pause. */
  els.resume.addEventListener('click', function () {
    els.resumeNote.textContent = 'resuming…';
    window.setTimeout(function () {
      if (els.resumeNote.textContent === 'resuming…') {
        els.resumeNote.textContent = '';
      }
    }, 4000);
    post('/admin/resume', {});
  });

  /* Mark issue: a timestamped line in the event log at the moment the
   * operator sees something, with whatever they can type in two seconds.
   * On 2026-10-24 "somewhere after half an hour" was the best timestamp
   * anyone had. */
  els.mark.addEventListener('click', function () {
    var note = els.markNote.value;
    els.markSent.textContent = 'marked';
    window.setTimeout(function () { els.markSent.textContent = ''; }, 2000);
    post('/admin/mark', {note: note});
    els.markNote.value = '';
  });

  els.screenView.addEventListener('change', pushScreen);
  els.screenSize.addEventListener('change', pushScreen);
  [els.screenLogo, els.screenGlow, els.screenFooter, els.screenLabels,
   els.screenHighlight]
    .forEach(function (box) { box.addEventListener('change', pushScreen); });
  /* On `change`, not on every keystroke: `change` fires when the field is
   * left or Enter is pressed, which is one save per edit rather than one per
   * letter - and this writes a file. */
  els.screenCredit.addEventListener('change', pushScreen);
  /* `change`, not `input`: the operator is typing a sentence that a room
     will read, and pushing every keystroke would put half-written words on
     a projector. It fires on blur and on Enter, which is when they have
     finished - and on blur alone for the subline, whose Enter is a line
     break rather than a commit. That is what the note under these fields
     says. */
  [els.screenEyebrow, els.screenHeadline, els.screenSubline,
   els.screenPill, els.screenCta, els.screenNoteLine,
   els.screenInvite]
    .forEach(function (field) {
      field.addEventListener('change', pushScreen);
    });

  els.source.addEventListener('change', showRows);
  els.refreshDevices.addEventListener('click', refreshDevices);

  /* So the top line answers "will this be saved?" the moment the box moves,
   * rather than at the next poll. */
  els.record.addEventListener('change', function () {
    renderRecording({running: false});
  });

  els.start.addEventListener('click', function () {
    var params = startParams();
    if (!params) { return; }
    if (mode() === 'live') {
      /* Two deliberate actions before any credit is spent: the checkbox, and
       * then this. An inline bar rather than confirm(), because a modal
       * dialog blocks every later instruction the page receives - including
       * the Stop button. */
      if (!els.arm.checked) {
        fail('Tick "I mean it" first, a live run uses paid minutes.');
        return;
      }
      els.confirmBar.hidden = false;
      return;
    }
    post('/admin/start', params);
  });

  els.confirmYes.addEventListener('click', function () {
    var params = startParams();
    hideConfirm();
    if (params) { post('/admin/start', params); }
  });

  els.confirmNo.addEventListener('click', hideConfirm);

  els.stop.addEventListener('click', function () {
    hideConfirm();
    post('/admin/stop');
  });

  showRows();
  loadDetails();
  loadTranscripts();
  loadDevices();
  poll();
  window.setInterval(poll, POLL_MS);
  window.setInterval(pollMeter, METER_MS);
}());
