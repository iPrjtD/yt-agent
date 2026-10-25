# Next steps (updated 2026-10-25)

## Done

- One Buzzbee voice for every video (Gemini TTS `voice.host_voice`, default Puck). Each take is checked
  with Whisper (rejected if extra words were spoken); without a good take the Short is postponed, never
  voiced differently. gemini-3.8-flash-tts gets the plain script (it reads tags/directions aloud).
- Talking, blinking Buzzbee host in the corner for the whole video.
- Shot-by-shot visuals: the edit planner gives each ~3 s phrase its own footage search and scene; cuts are
  timed to the spoken words. Never an unrelated clip: AI scene, or hold the previous shot.
- Narration slightly sped up (max 8%, pitch kept) when a take would exceed 59 s; scripts 100-130 words.
- Overnight Produce tasks re-enabled.

## Waiting on the user

1. Pick Buzzbee's voice from `data/buzzbee_voice_{Puck,Leda,Zephyr,Fenrir}.mp3` and set `voice.host_voice`
   in `config/settings.yaml`; then delete the sample files.
2. Free Cloudflare account for AI cartoon scenes: put `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`
   (Workers AI permission) in `.env`. The code (`providers/images.py`, FLUX.1 schnell, daily cap 120) activates
   automatically.
3. Music tracks in `assets/music/<mood>/`; submit the YouTube API audit (`docs/YOUTUBE_API_AUDIT.md`).
