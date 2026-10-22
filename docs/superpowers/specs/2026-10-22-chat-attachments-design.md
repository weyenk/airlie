# Chat attachments: pictures, video and any file from the + (design, 2026-10-22)

Follow-up to AIR-36 (PR 125) and PR 126. Noah, reviewing the Chat screen: the + beside the composer should
let a person attach a picture, video or file, and he chose "everything" (not images only). Today the + opens
Start new work. That stays reachable from the quick reply and the nav.

## What the engine can take

The chat role runs on `generalist` (Qwen3.8-27B, `tags: [vision]`), which vllm-mlx 0.5.0 serves on its MLLM path.
Its OpenAI route takes these content parts (checked in `vllm_mlx/api/utils.py` and `models/mllm.py`):

- `image_url` with a `data:image/...;base64` URL. It decodes up to 20 MB.
- `video_url` with a `data:video/...;base64` URL. It decodes up to 500 MB, samples frames itself with OpenCV
  (installed), and reads `video_fps` / `video_max_frames` from the request body.
- `audio_url`, but only for omni models. Qwen3.8 is not one.

So pictures and video reach the model as pixels. Any other file reaches it only as text, or as a line that
says what the file is.

## The flow

1. **Attach.** The + becomes an Attach button (paperclip, "Attach a file") that opens the device's file picker
   with `multiple`. On iOS that is Photo Library, Take Photo or Video, or Choose File, and Safari hands photos
   over as JPEG. On desktop you can also drop files on the composer or paste them. Each file uploads as soon as
   it is picked, as a chip above the box: a thumbnail for a picture, an icon and name for anything else, a
   progress bar while it uploads and a × to remove it. Send waits until every chip has finished uploading.
   A chip that failed (too large, for example) says why, and Send stays off until it is removed.
   The composer reads the limits from `GET /v1/airlie/chat/attachments` and refuses a file that is too large
   before uploading any of it. The server checks again.
2. **Upload.** `POST /v1/airlie/uploads?name=<file name>` takes the raw bytes, with the file's type as
   Content-Type. It is behind sign-in like every other `/v1` route. The server streams the body to
   `state_dir/uploads/<id>` (id = 32 random hex characters, never a hash of the content) and writes
   `<id>.json` beside it: `{id, name, mime, size, kind, owner, at}`. It stops at the kind's limit with a 413
   that names the limit. `kind` is decided on the server, from the bytes' signature and the name's extension,
   not from the browser's word:
   - `image`: png, jpeg, webp or gif.
   - `video`: mp4, mov or webm.
   - `text`: anything that decodes as UTF-8, such as code, logs, csv, json, md or yaml.
   - `pdf`: its text is pulled out with `pypdf`, a new pure-Python dependency.
   - `file`: everything else (zip, docx, audio, binaries). It is accepted and stored, but its contents are
     not read.

   `GET /v1/airlie/uploads/<id>` returns the bytes, to the person who uploaded the file (or an admin) only.
   Anyone else gets a 404, the same as for an id that does not exist.
3. **Send.** A user message with attachments goes to `/v1/chat/completions` as a content list:
   `[{"type": "text", "text": "..."}, {"type": "airlie_upload", "upload": "<id>"}, ...]`. The chat route turns
   each `airlie_upload` part into what the model can take:
   - `image` becomes an `image_url` data URL.
   - `video` becomes a `video_url` data URL. The request gets `video_fps` and `video_max_frames` from config
     (default 1 fps and 32 frames, so a long clip stays inside the context).
   - `text` and `pdf` become a text part: ``Attached file `name` (12 KB):`` and then the text in a fence. The
     text is cut at `max_text_kb` (default 200) with a note saying it was cut and how big the file is. A PDF
     with no text layer (a scan) says so.
   - `file` becomes one line: ``Attached file `name` (application/zip, 3.1 MB): its contents can't be read
     from chat.`` The chat prompt says the model should tell the person what it can and cannot see.
   - An id that is gone (expired or never there) becomes ``[attachment `name` is no longer available]``. The
     conversation goes on instead of failing.
   - An `airlie_upload` part sent to any model other than `chat` is a 400. The chief, raw catalog models and
     OpenCode are unchanged.
4. **History.** The thread in sessionStorage keeps only `attachments: [{id, name, kind, mime, size}]` on the
   user's turn, never bytes, so a 100 MB video doesn't break the 5 MB sessionStorage limit. After a reload,
   thumbnails are fetched again from `GET /uploads/<id>`. Each request re-sends the turns' upload ids, and the
   server inlines them. Only the most recent 4 pictures and 1 video in the history go to the model as pixels.
   Older ones become `[earlier image `shot.png`]`, so a video isn't re-encoded on every later turn. `/clear`
   empties the thread and leaves the stored files to expire.
5. **Keeping.** At startup, and once a day while serving, files older than `keep_days` (default 30) are
   deleted.

## Config (airlie.yaml)

```yaml
chat:
  attachments: { max_image_mb: 20, max_video_mb: 100, max_file_mb: 25, max_text_kb: 200, max_per_message: 10,
                 keep_days: 30, video_fps: 1.0, video_max_frames: 32, history_images: 4, history_videos: 1 }
```

The video default is 100 MB because the Cloudflare quick tunnel refuses bodies over 100 MB. Tailscale has no
such limit, so you can raise it in `airlie.local.yaml`. The chat role gets `vision: true`.

## Out of scope

- Attachments carried into `/story` or Start new work. The story card's text can describe what the model saw,
  but the story pipeline takes text and a repo only. That would be its own story.
- Audio understanding. It needs an omni model.
- Reading inside docx, xlsx or zip files. Those are `file` in v1 and could be added kind by kind later.
- Deleting files on `/clear`.

## Tests

**Unit** (`tests/unit/test_serve_uploads.py`):

- An upload stores the bytes and a meta file, and returns `{id, name, kind, mime, size}`.
- `kind` comes from the bytes: a `.png` name on zip bytes is `file`.
- Each kind's limit gives a 413 that names the limit, and leaves no partial file behind.
- Without sign-in the route is a 401. Another person's id is a 404.
- The keep sweep deletes old files.
- The chat route turns each kind into its part, as described above, and sets `video_fps` / `video_max_frames`.
- Text is cut at the limit with the note. PDF text is extracted, using a fixture PDF.
- A missing id becomes the placeholder. Pictures and videos past the history caps become `[earlier ...]`.
- A non-chat model with an `airlie_upload` part is a 400.

**e2e** (`tests/e2e/test_chat.py`, Chromium, stubbed upstream, a screenshot per check):

- The + opens the file chooser and no longer goes to Start new work. The quick reply still does.
- A PNG shows a thumbnail chip. Sent, it shows in my bubble, and the upstream got an `image_url` with exactly
  those bytes.
- A small mp4 gives a `video_url` part and the frame settings.
- A `.txt` file reaches the upstream as a fenced text part with its name.
- A zip shows a chip and gives the "can't be read" line.
- A file over the limit gives a chip with the size message, Send stays off, and × clears it.
- After a reload, the thumbnail is still in the thread, and sessionStorage holds no `base64`.

**Live (AC5-style), from a clean state on the Mac:** ask about an iPhone photo and a short iPhone video
(HEVC .mov), and attach a log file and a PDF. Check that the answers show the model saw each one.
