# What is private

Gitignored, and deliberately so. None of it has history to recover it from, so
back up anything you care about.

| Path | What it holds |
| --- | --- |
| `.env` | the API key and the admin token |
| `config/brand.json`, `brand/` | your branding |
| `config/session.yaml` | speaker names, the talk title, your notes |
| `config/screen.yaml` | the operator's saved screen wording and join link |
| `config/qr.svg`, `config/qr.png` | your own QR artwork |
| `config/mic.yaml` | which input this laptop last opened |
| `transcripts/` | a verbatim record of each session |
| `logs/`, `recordings/` | response logs, event logs, saved audio |
| `tests/fixtures/*.jsonl` | captures, which are transcripts too |

Committed example files show the shape of each one: `.env.example`,
`config/brand.example.json`, `config/session.example.yaml`.

Three things follow from that list, and the suite holds each one:

- **The audience pages are told nothing.** Speaker names and your notes go to
  the engine as context and to `/admin`, never to the viewer socket.
- **Audio is not recorded unless you tick the box.** The room has not consented
  to being recorded.
- **No artwork and no join link is committed**, so a fork carries the neutral
  theme rather than somebody's identity. See [branding.md](branding.md).
