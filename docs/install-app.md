# Installing Joven — the five-minute version

This page is for reading a novel, not for programming. There is no terminal in
it. If you would rather install from PyPI and work from the command line, the
[README](../README.md) covers that instead.

You need two things: **Joven** (one download) and **Ollama** (one download, plus a
model). Ollama is the part that does the translating, and it runs on your own
machine — nothing about your book is ever sent anywhere.

---

## 1. Download Joven

Go to the [latest release](https://github.com/vyanhursky/joven/releases/latest)
and take the file for your computer:

| Your computer | The file |
|---|---|
| Windows | `Joven-windows-x64.zip` |
| Mac (Apple Silicon — M1 and later) | `Joven-macos-arm64.dmg` |
| Linux | `Joven-linux-x64.tar.gz` |

It is a large download — about 220 MB — because Joven carries everything it needs
except the translation model. Most of that size is the language detector that
decides which sentences are Spanish.

**Windows:** right-click the `.zip` → *Extract All*. Keep the whole `Joven`
folder together; `Joven.exe` needs the files beside it.

**Mac:** open the `.dmg` and drag `Joven` into the `Applications` folder shown
beside it. Do not double-click Joven inside the disk image — an app run from
there cannot be approved, and the warning below will have no way past it.

**Linux:** `tar -xzf Joven-linux-x64.tar.gz`, then run `Joven/Joven`.

## 2. Get past the "unknown developer" warning

Joven is not code-signed — that requires a paid certificate from Microsoft and
Apple — so both systems will warn you the first time. This is expected, and you
only do it once.

**Windows.** You will see a blue box: *"Windows protected your PC"*. Click **More
info**, then **Run anyway**. If you would rather check first, right-click the zip
→ *Properties* → tick **Unblock** before extracting.

**Mac.** Make sure you have dragged Joven to **Applications** first — this does not
work from inside the disk image. Then:

1. Double-click **Joven** in Applications. macOS says *"Joven" Not Opened* — *Apple
   could not verify "Joven" is free of malware*. Click **Done**. This refusal is
   the expected first step, not a failure.
2. Open **System Settings → Privacy & Security** and scroll to the bottom. There is
   now a line saying Joven was blocked, with an **Open Anyway** button. Press it and
   authenticate.
3. Double-click Joven again. It opens, and it keeps opening from then on.

> On older versions of macOS there is a shorter route: **right-click** (or
> Control-click) the app and choose **Open**, which offers an **Open** button in the
> warning. Recent macOS removed it — if right-click → Open gives you no Open button,
> use the three steps above.

## 3. Start Joven

Double-click it. A page opens in your browser, at an address starting
`127.0.0.1` — that is your own machine, not a website. Joven is not online, and
press **Quit**, top right of the page, when you are done with it.

Quit is worth knowing about rather than guessing at. On Windows you can also close
the black window; on a Mac there is nothing else — Joven has no Dock menu and does
not answer Cmd-Q, so the button on the page is the off switch. Leaving it running
is harmless, and starting Joven again while it is already running just reopens the
page rather than starting a second copy.

The page opens on the **Setup** tab, which tells you what is still missing. The
first time, that will be the model.

## 4. Install Ollama and get the model

Ollama is what actually reads the Spanish. Download it from
[ollama.com/download](https://ollama.com/download) and install it the normal way
for your system. It runs quietly in the background.

Then go back to Joven's **Setup** tab and press **Check again**. Once Ollama is
found, a **Pull model** button appears. Press it.

This downloads about 6 GB and takes a while — it is a real language model. You can
watch the progress, and stopping it is safe: pressing *Pull model* again picks up
where it left off rather than starting over.

When Setup says **Ready**, you are done installing. You never have to do any of
this again.

## 5. Annotate a book

1. **Books** tab — drag your EPUB onto the page. It is copied into Joven's own
   folder and the original is never modified.
2. **Detect** — press *Start detect* and leave it. Expect roughly fifteen minutes
   for a novel; the progress bar shows where it is. You can cancel and resume.
3. **Review** — optional, and worth it. Joven puts the translations it is least
   sure about first. `a` approves, `r` rejects, `e` edits.
4. **Render** — press the button. Joven builds the annotated book, checks it, and
   offers two downloads. **Take the `.kepub.epub` one** for a Kobo.
5. Plug in your Kobo by USB and copy that file into the drive that appears. Eject,
   and it is on your reader.

Tap any asterisk in the text to see the translation.

---

## If something goes wrong

The **Setup** tab is the first place to look — it checks every part and says what
each missing piece costs you. Two of its warnings are worth explaining, because
neither stops you reading:

**"kepubify — not found"** should not happen in the app, which carries its own
copy. If you see it, the download was probably extracted incompletely: extract the
whole folder again.

**"java — not found"** is normal and harmless. Joven runs twelve checks on the
book it produces; eleven are built in, and the twelfth uses an external validator
written in Java. Without Java that one is skipped. The book is fine.

Anything else: [open an issue](https://github.com/vyanhursky/joven/issues) and
paste what the Setup tab says.

## What Joven does not do

It does not go online, except when Ollama downloads the model. It does not send
your book anywhere. It does not modify the file you gave it. And it will refuse a
DRM-protected book outright, because it cannot read one — buy the EPUB, or use one
you already own.
