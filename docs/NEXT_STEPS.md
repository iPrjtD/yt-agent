# Next steps (updated 2026-10-25)

## Done

- One Buzzbee voice for every video (Gemini TTS `voice.host_voice` = Puck, chosen by the user). Each take is checked
  with Whisper (rejected if extra words were spoken); without a good take the Short is postponed, never
  voiced differently. gemini-3.8-flash-tts gets the plain script (it reads tags/directions aloud).
- Talking, blinking Buzzbee host in the corner for the whole video.
- Shot-by-shot visuals: the edit planner gives each ~3 s phrase its own footage search and scene; cuts are
  timed to the spoken words. Never an unrelated clip: AI scene, or hold the previous shot.
- Narration slightly sped up (max 8%, pitch kept) when a take would exceed 59 s; scripts 100-130 words.
- Overnight Produce tasks re-enabled.
- AI cartoon scenes live (Cloudflare Workers AI, FLUX.1 schnell, claymation style): drawn first for historical
  moments and whenever real stock is weaker than a strong match (CLIP 0.27). Daily cap 120.

## Waiting on the user

1. Music tracks in `assets/music/<mood>/`; submit the YouTube API audit (`docs/YOUTUBE_API_AUDIT.md`).
