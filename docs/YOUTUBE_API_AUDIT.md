# YouTube API audit: draft answers

Until Google audits the API project, videos uploaded through the API stay locked as private.
Submit the **YouTube API Services – Audit and Quota Extension Form**:
https://support.google.com/youtube/contact/yt_api_form

Sign in with the Google account that owns the Buzzbee channel and the `yt-agent` Cloud project.
Answers below; adjust anything that is not accurate for you.

## Organization / developer

- Developer name: Priyajit Dolai (individual developer)
- Contact email: the Gmail that owns the channel
- Website / homepage: https://iprjtd.github.io/
- Privacy policy: https://iprjtd.github.io/privacy.html

## API client

- Google Cloud project: `yt-agent`
- Project number: the number at the start of the OAuth client ID (Cloud Console → Dashboard → Project info)
- Client type: installed (desktop) application, used only by the developer
- APIs: YouTube Data API v3, YouTube Analytics API
- OAuth scopes: `https://www.googleapis.com/auth/youtube`, `https://www.googleapis.com/auth/yt-analytics.readonly`
- Number of users: 1 (the channel owner). Not offered to the public.

## Use case (paste into the description field)

> yt-agent is a private, single-user tool that helps me run my own YouTube channel, Buzzbee
> (channel ID UCzODU-zkEJBaUqBcUwBzoig). It runs only on my own computer and only accesses my own channel.
>
> It uses the YouTube Data API to: upload videos I produced to my channel as private drafts, add them to a
> private "Pending review" playlist, update their title/description/tags, and publish or schedule them only
> after I approve them by moving them to an "Approved" playlist in YouTube Studio. It also reads the public
> mostPopular chart (chart=mostPopular) to see what is trending, and manages my channel's playlists and branding.
>
> It uses the YouTube Analytics API to read my own channel's reports (views, watch time, retention,
> subscribers) to learn which of my videos perform best.
>
> No data from other users or channels is stored, shared or sold. Tokens and analytics stay on my computer.
> No other users use the tool. Expected volume: about 2-3 uploads per day, well within the default quota.

## Quota

- Requesting: no extra quota (default 10,000 units/day is enough); the request is to lift the private-only
  restriction on uploads.

## Evidence they may ask for

- Screenshots or a short screen recording of: the command-line tool producing a Short, the private upload
  in YouTube Studio inside "Pending review", and the approval playlist flow.
- Link to the privacy policy above.

## After approval

Set `publishing.mode: api` in `config/settings.yaml`. Uploads then go to "Pending review" and the agent
publishes what you move to "Approved".
