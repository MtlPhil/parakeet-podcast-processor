# Trying P³ on Linux or Windows

P³ was built on a Mac. On Linux and Windows it runs in a Docker container
and transcribes with [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
on the CPU. Every change is checked by tests, and CI builds the container and
starts it, but nobody has transcribed a real episode with it yet. That is
what this test is for. The timings you note down (see the end) will choose
the default speech model for these machines.

You need about 10 GB of free disk space and 8 GB of RAM (16 GB if you also
run a local model with Ollama).

## 1. Install the tools

**Linux:** [Docker Engine](https://docs.docker.com/engine/install/) and git
(`sudo apt install git` on Debian or Ubuntu). To run `docker` without
`sudo`, add yourself to the `docker` group and log in again.

**Windows 10 or 11:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(keep the default WSL 2 backend) and [Git for Windows](https://git-scm.com/download/win).
Start Docker Desktop once and wait until it says it is running. Run the
commands below in PowerShell.

## 2. Choose what writes the summaries

Pick one:

- **Gemini (simplest).** Get a free API key at
  [Google AI Studio](https://aistudio.google.com/apikey). You paste it into
  P³'s setup page in step 4. Nothing else to install.
- **Ollama (everything stays on the machine).** Install
  [Ollama](https://ollama.com/download) on the machine itself, not in Docker,
  then pull a model and Ask's embedding model:

  ```bash
  ollama pull gemma4:12b
  ollama pull bge-m3
  ```

  `gemma4:12b` needs about 8 GB of memory while it runs.

## 3. Build and start P³

```bash
git clone https://github.com/MtlPhil/parakeet-podcast-processor.git
cd parakeet-podcast-processor
docker build -t p3 .
```

The build takes a few minutes. Then start P³:

| Machine | Command |
|---|---|
| Windows, or Linux with Gemini | `docker run -d --name p3 --restart unless-stopped -p 8000:8000 -v p3-library:/library p3` |
| Linux with Ollama | `docker run -d --name p3 --restart unless-stopped --network host -v p3-library:/library p3` |

On Linux, `--network host` lets P³ reach Ollama at `localhost`, where Ollama
listens by default. Everything P³ keeps (database, settings, transcripts,
downloaded models) is in the `p3-library` volume. It survives updates and
restarts.

## 4. Set it up

Open <http://localhost:8000>. The setup page walks you through:

1. **Your account.** The first account is the admin.
2. **Summaries.** Choose Gemini and paste the key, or, on Linux, choose
   Ollama and pick the model. With Ollama on Windows, skip this page: the
   container reaches Ollama at another address, set after the setup (below).
3. **Transcription.** Leave "Use Parakeet" as it is (it is Mac only and is
   skipped here). Keep Whisper quality at **Balanced** for the first test.
4. **Daily digest, who can open P³.** Skip, or set them as you like.
5. **Your podcasts.** Set "Episodes per podcast" (on the Transcription
   page) to 1 first, so the test is a single episode. With Ollama on
   Windows, skip this page too and add the podcast once Ollama is set.

**Ollama on Windows:** open Settings, and under Shared settings set the
summarizing provider to Ollama, the model to `gemma4:12b` and the Ollama URL
to `http://host.docker.internal:11434`, then save. Add your podcast from
Add Podcast.

The Whisper model can be changed later in the same place.

## 5. Watch the first episode

Adding the podcast starts a fetch. P³ then transcribes the episode,
summarizes it and translates the summary, one job after another, shown on
the Dashboard. The first transcription also downloads the speech model
(about 1.6 GB for Balanced), so it is slower.

To see what P³ is doing:

```bash
docker logs -f p3
```

## 6. What to send back

- The machine: OS, CPU, RAM, GPU if any.
- The episode's length and how long its transcription took. The log has a
  `Transcribing:` line when it starts and the next job's lines when it
  ends. Leave out the first episode, which includes the model download, or
  note it apart.
- Whether the summary reads well and the transcript looks right.
- If anything failed: the error shown on the Dashboard, and the log saved
  with `docker logs p3 > p3-log.txt`.

If there's time, the same episode with Whisper quality set to "Lighter"
(`small`) would show how much faster it is. Change it in Settings, delete the
episode on its page, and add it again from the podcast's page (Add Past
Episodes).

## Updating, stopping, removing

```bash
cd parakeet-podcast-processor
git pull
docker build -t p3 .
docker rm -f p3
```

Then run the same `docker run` command as in step 3. Your library is kept.

- Stop P³: `docker stop p3`. Start it again: `docker start p3`.
- Remove everything, the library included: `docker rm -f p3`, then
  `docker volume rm p3-library`.

## Not covered yet

- An NVIDIA GPU: `--gpus all` reaches the container, but the image has no
  CUDA libraries, so transcription stays on the CPU.
- Telling speakers apart and Ask's reranker: their extras (PyTorch) are not
  in the image.
- Running P³ on Windows without Docker: possible in principle (the manual
  installation in the README), but never tried.
