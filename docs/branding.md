# Branding

Nothing in this repo carries anyone's identity. Out of the box it runs on a
neutral theme: a blue accent chosen for contrast, a near-black projector
background, and generic wording on the waiting screen.

Your own branding goes in two places, both gitignored, so it never ends up in a
fork or in this repo's history.

## Words and colours

```sh
cp config/brand.example.json config/brand.json
# PowerShell: Copy-Item config\brand.example.json config\brand.json
```

```json
{
  "event_name": "Thursday Night AI",
  "accent": "#4DA3FF",
  "accent_ink": "#0B0B0D",
  "background": "#0B0B0D",
  "event_url": "https://example.com/join",
  "waiting": {
    "eyebrow": "LIVE CAPTIONS",
    "headline": "Captions start shortly.",
    "subline": "Arabic and English captions, side by side,\nas the talk happens.",
    "pill": "Scan to join",
    "cta": "Scan to open the link",
    "note": "Opens the event link"
  }
}
```

Every key is optional; delete one and the neutral default applies. Colours are
six-digit hex and are validated, so a typo falls back rather than breaking the
stylesheet.

**`accent` and `accent_ink` are a pair.** The accent carries text and has text
drawn on it. If you pick something pale, set `accent_ink` to something dark or
the label on it becomes unreadable. The test suite asserts both directions
clear WCAG AA, so a theme that breaks it fails the suite rather than the room.

The waiting screen's wording layers: these values beat the built-in defaults,
and anything typed on `/admin` beats these. So an event sets its wording once
and still lets the operator change it on the day.

## Images

Drop files into `brand/` under these exact names. Any you leave out are hidden
rather than drawn broken.

| File | Where it appears |
| --- | --- |
| `brand/mark.png` | the square mark in both page headers |
| `brand/wordmark-en.png` | the Latin wordmark, on the title card |
| `brand/wordmark-ar.png` | the Arabic wordmark, beside it |
| `brand/favicon-32.png` | the browser tab |
| `brand/apple-touch-icon-180.png` | a phone home screen |

With no wordmark artwork at all, the title card sets `event_name` in type
instead. With artwork, the typed name is not shown, because you have already
said how your name should look.

The wordmarks are drawn white on transparent and only ever appear on the dark
projector view. After adding or replacing them:

```sh
uv run --with pillow python tools/brand_assets.py
```

That measures the files and writes `web/brand-sizes.css`, so the title card is
sized from your artwork rather than from a number in the stylesheet. Larger
artwork makes the card larger by itself. It is optional: without it the layout
uses sane defaults.

## A join link

The waiting screen's card always holds something. In order of precedence:
`config/qr.svg` or `config/qr.png` if you drop one in, served byte for byte;
otherwise a code drawn from the link you type on `/admin`; otherwise
`event_url`. All gitignored. A real invite link is an open door for anyone
holding it, so it never leaves the laptop and never reaches the viewer socket.

What else stays on the laptop: [privacy.md](privacy.md).
