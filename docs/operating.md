# Operating a session

Two pages: one for the room, one for you.

## `/screen` - the projector

Open `http://127.0.0.1:8000/screen`, drag the window onto the projector and
make it fullscreen. No controls, no cursor, nothing to click. It has three
states and switches between them on its own:

- **Waiting.** Before Start and after Stop. An eyebrow, a headline, a subline,
  and a card that can hold a QR code for a join link.
- **Title card.** Running, but with nothing to show yet - the gap between the
  first word and its translation. A room that has just heard "we are starting"
  should not be looking at a come-and-join-us poster.
- **Captions.** The talk.

Query parameters are testing hatches, not settings: `?lang=en|ar|both|auto`
pins the view against the operator, `?size=s|m|l|xl` the text size,
`?card=1|0` holds the title card up or down, `?glow=0` turns the background
wash off.

## `/admin` - the operator

`http://127.0.0.1:8000/admin?token=YOUR_TOKEN`. One page, from one laptop,
behind a constant-time token check. It is never linked from anywhere.

- **Start / Stop**, with a confirmation step before a live run because a live
  run spends money.
- **Audio.** Input device, level meter, and whether to save the audio to the
  laptop. Saving is off unless you tick it: the room has not consented to being
  recorded.
- **Main screen.** View, text size, the corner mark, the footer disclaimer, the
  background glow, the credit line, and every word on the waiting screen.
  Changes reach an open projector immediately, without touching that browser.
- **Session.** The talk's title, speakers, companies and your own notes. These
  are fed to the engine as context, which is what makes names come out spelled
  right. They are saved to `config/session.yaml`, which is gitignored, and they
  never reach the audience pages, the viewer socket or any log.
- **Transcripts.** Every live session writes `english.md`, `arabic.md`,
  `bilingual.md` and a stats sheet into `transcripts/`, readable and
  downloadable here and nowhere else.
- **Diagnostics.** Mark a moment when something looks wrong, and download one
  session's logs as a zip.

Choosing an input device is in [setup.md](setup.md#5-choose-the-input).

## Without the page

A session can also be driven over HTTP:

```sh
curl -X POST "http://127.0.0.1:8000/admin/start?token=YOUR_TOKEN&capture=run1.jsonl&paced=true"
curl -X POST "http://127.0.0.1:8000/admin/stop?token=YOUR_TOKEN"
```

`capture=` replays a saved file instead of opening the microphone, which costs
nothing and reaches no network. `paced=true` replays at real time, including
caption lag; `paced=false` pushes the whole file through in under a second.

## Cost and spend control

Soniox bills audio and text tokens separately. With two-way translation
running, this project measured roughly **$0.20 per hour of live audio** on its
own usage, so a two-hour evening is about 40 cents.

Your figure moves with how much is actually said, so check
[soniox.com/pricing](https://soniox.com/pricing) against your own invoice
rather than trusting the number above. `SONIOX_RATE_PER_MINUTE` in `.env` feeds
the running spend estimate on `/admin`; it is a counter, not a cap.

Three things keep the bill honest:

- **Replay costs nothing.** Development, tests and judging the screen all run
  off saved files.
- **A session that goes quiet pauses itself** rather than streaming silence,
  with a banner on `/admin` saying what it is waiting for and a Resume button.
- **Minutes are logged** to `logs/usage.jsonl`, with the running total on
  `/admin`.

Only a live session and the two paid tools in `tools/` ever reach Soniox. The
entire test suite does not.
