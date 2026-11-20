# Deploying the live demo (real Apply and Rollback)

`hosted_server.py` serves the same dashboard as `python greendata.py serve`, but on a public server.
Each visitor gets a private temporary sandbox on the server. Apply and Rollback really change files there,
never on the visitor's own computer. No packages to install.

Try it on your own machine first:

```
python hosted_server.py
```

Open http://127.0.0.1:8000, press **Create demo folder on server and scan**, then Apply, then Roll back.

## Option A: Render (free, needs only a GitHub login)

1. Push this repo to GitHub (the files `hosted_server.py`, `hosted_ui.html`, `render.yaml` must be included).
2. Go to https://render.com, sign in with GitHub, press **New +**, then **Blueprint**, and pick this repo.
3. Render reads `render.yaml`. Press **Apply**. After about 2 minutes you get a link like `https://greendata-ai.onrender.com`.
4. Put that link in the README next to the GitHub Pages link.

If you prefer clicking through manually: **New + > Web Service**, runtime **Python 3**, build command
`echo ok`, start command `python hosted_server.py`, health check path `/healthz`.

## Option B: Hugging Face Spaces (free, no sleep warning page)

1. https://huggingface.co/new-space, SDK **Docker**, blank template.
2. Upload (or push) these files: `Dockerfile`, `greendata.py`, `greendata_prototype.html`, `hosted_server.py`, `hosted_ui.html`.
3. Add this block at the very top of the Space's `README.md`:

```
---
title: GreenData AI
sdk: docker
app_port: 7860
---
```

4. The Space builds and gives you `https://<user>-<space>.hf.space`.

## Things to know

- Free plans sleep when idle. The first visit after a sleep takes 30 to 60 seconds. Open the link once before a demo.
- Sandboxes are temporary: deleted after 60 minutes idle (`GD_TTL_MINUTES`) and wiped when the server restarts.
- Limits per visitor: 60 files, 5 MB in total, 3 heavy jobs at a time on the whole server.
- The page tells visitors not to upload private data. Keep that notice.
- The GitHub Pages demo stays as it is. It is a browser-only simulation. This server is the version where Apply is real.
