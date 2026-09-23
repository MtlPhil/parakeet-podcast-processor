"""Command-line interface for P³."""

import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import track
from rich.table import Table

from .cleaner import TranscriptCleaner
from .database import P3Database
from .downloader import PodcastDownloader
from .exporter import DigestExporter
from .llm import DEFAULT_OLLAMA_URL, SUPPORTED_PROVIDERS, resolve_provider_and_model
from .transcriber import DEFAULT_PARAKEET_MODEL, AudioTranscriber
from .writer import BlogWriter
from .youtube import YouTubeError

console = Console()

logger = logging.getLogger("p3")


def _setup_logging(verbosity: int):
    """Configure logging based on verbosity level.

    0 = quiet  (WARNING and above)
    1 = normal (INFO and above, default)
    2 = verbose (DEBUG and above)
    """
    level = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}.get(
        verbosity, logging.DEBUG
    )
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
    # Suppress noisy third-party loggers at DEBUG level
    if verbosity >= 2:
        for name in ("httpx", "httpcore", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)


def load_config(config_path: str = "config/feeds.yaml"):
    """Load configuration from YAML file."""
    config_file = Path(config_path)

    if not config_file.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        console.print(
            "Copy config/feeds.yaml.example to config/feeds.yaml and configure your feeds"
        )
        sys.exit(1)

    try:
        with open(config_file, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        console.print(f"[red]Error loading config: {e}[/red]")
        sys.exit(1)


def _check_prerequisite(name: str, cmd: list) -> bool:
    """Check whether an external tool is available."""
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


@click.group()
@click.option("--config", default="config/feeds.yaml", help="Configuration file path")
@click.option("--db", default="data/p3.duckdb", help="Database file path")
@click.option("-v", "--verbose", count=True, help="Enable debug logging")
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress all output except warnings/errors"
)
@click.pass_context
def main(ctx, config, db, verbose, quiet):
    """Parakeet Podcast Processor (P³) - Automated podcast processing."""
    ctx.ensure_object(dict)

    # Resolve verbosity: -q wins over -v
    if quiet:
        verbosity = 0
    else:
        verbosity = max(1, verbose + 1)  # default is 1 (INFO), -v => 2 (DEBUG)

    _setup_logging(verbosity)

    ctx.obj["config_path"] = config
    ctx.obj["db_path"] = db
    ctx.obj["db"] = P3Database(db)


@main.command()
@click.option("--max-episodes", default=None, type=int, help="Max episodes per feed")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be downloaded without downloading"
)
@click.pass_context
def fetch(ctx, max_episodes, dry_run):
    """Download new episodes from configured feeds.

    Feeds can be RSS URLs or YouTube channel, video or playlist URLs.
    """
    config = load_config(ctx.obj["config_path"])
    db = ctx.obj["db"]

    settings = config.get("settings", {})
    max_eps = max_episodes or settings.get("max_episodes_per_feed", 10)

    feeds = config.get("feeds", [])
    if not feeds:
        console.print("[yellow]No feeds configured[/yellow]")
        return

    downloader = PodcastDownloader(
        db=db, max_episodes=max_eps, audio_format=settings.get("audio_format", "wav")
    )

    if dry_run:
        console.print(
            "[blue]Dry run — listing episodes that would be downloaded:[/blue]"
        )
        table = Table(title="Episodes to Download")
        table.add_column("Podcast", style="cyan")
        table.add_column("Episode", style="white")
        table.add_column("Date", style="green")

        for feed_config in feeds:
            name = feed_config["name"]
            url = feed_config["url"]
            try:
                episodes = downloader.fetch_episodes(url, limit=max_eps)
            except (YouTubeError, ValueError) as e:
                console.print(f"[red]{name}: {e}[/red]")
                continue
            for ep in episodes:
                if not db.episode_exists(ep["url"]):
                    date_str = (
                        ep["date"].strftime("%Y-%m-%d") if ep["date"] else "unknown"
                    )
                    table.add_row(name, ep["title"], date_str)

        console.print(table)
        return

    console.print("[blue]Fetching podcast episodes...[/blue]")

    total_downloaded = 0
    results = downloader.fetch_all_feeds(feeds)

    table = Table(title="Download Results")
    table.add_column("Podcast", style="cyan")
    table.add_column("New Episodes", style="green", justify="right")

    for name, count in results.items():
        table.add_row(name, str(count))
        total_downloaded += count

    console.print(table)
    console.print(f"[green]Total downloaded: {total_downloaded} episodes[/green]")


@main.command()
@click.option("--model", default=None, help="Whisper model to use")
@click.option("--episode-id", type=int, help="Transcribe specific episode")
@click.pass_context
def transcribe(ctx, model, episode_id):
    """Transcribe downloaded audio files."""
    config = load_config(ctx.obj["config_path"])
    db = ctx.obj["db"]

    settings = config.get("settings", {})
    whisper_model = model or settings.get("whisper_model", "base")
    use_parakeet = settings.get("parakeet_enabled", False)

    transcriber = AudioTranscriber(
        db=db,
        whisper_model=whisper_model,
        use_parakeet=use_parakeet,
        parakeet_model=settings.get("parakeet_model", DEFAULT_PARAKEET_MODEL),
    )

    if episode_id:
        console.print(f"[blue]Transcribing episode {episode_id}...[/blue]")
        success = transcriber.transcribe_episode(episode_id)
        if success:
            console.print(f"[green]Episode {episode_id} transcribed[/green]")
        else:
            console.print(f"[red]Failed to transcribe episode {episode_id}[/red]")
    else:
        console.print("[blue]Transcribing all pending episodes...[/blue]")
        episodes = db.get_episodes_by_status("downloaded")

        if not episodes:
            console.print("[yellow]No episodes to transcribe[/yellow]")
            return

        transcribed = 0
        for episode in track(episodes, description="Transcribing..."):
            if transcriber.transcribe_episode(episode["id"]):
                transcribed += 1

        console.print(f"[green]Transcribed {transcribed} episodes[/green]")

    transcriber.unload_models()


@main.command()
@click.option(
    "--provider",
    default=None,
    type=click.Choice(SUPPORTED_PROVIDERS),
    help="LLM provider (overrides config)",
)
@click.option("--model", default=None, help="LLM model to use (overrides config)")
@click.option("--episode-id", type=int, help="Process specific episode")
@click.pass_context
def digest(ctx, provider, model, episode_id):
    """Generate structured summaries from transcripts."""
    config = load_config(ctx.obj["config_path"])
    db = ctx.obj["db"]

    settings = config.get("settings", {})
    llm_provider, llm_model = resolve_provider_and_model(settings, provider, model)

    cleaner = TranscriptCleaner(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        ollama_base_url=settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
    )

    if episode_id:
        console.print(f"[blue]Processing episode {episode_id}...[/blue]")
        result = cleaner.generate_summary(episode_id)
        if result:
            console.print(f"[green]Episode {episode_id} processed[/green]")
        else:
            console.print(f"[red]Failed to process episode {episode_id}[/red]")
    else:
        console.print("[blue]Processing all transcribed episodes...[/blue]")
        processed = cleaner.process_all_transcribed()
        console.print(f"[green]Processed {processed} episodes[/green]")


@main.command()
@click.option("--date", help="Export date (YYYY-MM-DD)")
@click.option("--format", multiple=True, help="Export format (markdown, json, html)")
@click.option("--output", help="Output file path")
@click.pass_context
def export(ctx, date, format, output):
    """Export daily digest summaries."""
    config = load_config(ctx.obj["config_path"])
    db = ctx.obj["db"]

    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            return
    else:
        target_date = datetime.now()

    formats = (
        list(format)
        if format
        else config.get("settings", {}).get("export_format", ["markdown"])
    )

    exporter = DigestExporter(db)

    summaries = db.get_summaries_by_date(target_date)

    if not summaries:
        console.print(f"[yellow]No summaries found for {target_date.date()}[/yellow]")
        return

    console.print(
        f"[blue]Exporting {len(summaries)} summaries for {target_date.date()}[/blue]"
    )

    for fmt in formats:
        if fmt == "markdown":
            content = exporter.export_markdown(summaries, target_date.date())
            filename = output or str(
                exporter.get_export_path(
                    f"digest_{target_date.strftime('%Y-%m-%d')}.md"
                )
            )
        elif fmt == "json":
            content = exporter.export_json(summaries, target_date.date())
            filename = output or str(
                exporter.get_export_path(
                    f"digest_{target_date.strftime('%Y-%m-%d')}.json"
                )
            )
        elif fmt == "html":
            content = exporter.export_email_html(summaries, target_date.date())
            filename = output or str(
                exporter.get_export_path(
                    f"digest_{target_date.strftime('%Y-%m-%d')}.html"
                )
            )
        else:
            console.print(f"[red]Unsupported format: {fmt}[/red]")
            continue

        with open(filename, "w") as f:
            f.write(content)

        console.print(f"[green]Exported {fmt}: {filename}[/green]")


def _write_transcript_file(
    episode_id: int, transcript: str, fmt: str, output_dir: str
) -> Path:
    """Write one transcript to output_dir in the given format."""
    if fmt == "markdown":
        content = f"# Transcript for Episode {episode_id}\n\n{transcript}"
        filename = f"transcript_{episode_id}.md"
    else:
        content = transcript
        filename = f"transcript_{episode_id}.txt"
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    file_path = Path(output_dir) / filename
    file_path.write_text(content)
    return file_path


@main.command()
@click.option(
    "--date",
    required=False,
    help="Export all transcripts for a given date (YYYY-MM-DD)",
)
@click.option(
    "--format",
    "fmt",
    default="txt",
    type=click.Choice(["txt", "markdown"]),
    help="Export format",
)
@click.option(
    "--output-dir", default="exports", help="Output directory for transcripts"
)
@click.pass_context
def export_transcript(ctx, date, fmt, output_dir):
    """Export transcript(s) for one episode, or for every episode on a date."""
    db = ctx.obj["db"]

    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            return

        episodes = db.get_episodes_by_date_and_status(target_date)
        if not episodes:
            console.print(f"[yellow]No episodes found for {target_date}[/yellow]")
            return

        exported = 0
        for episode in episodes:
            transcript = db.get_transcript_by_episode_id(episode["id"])
            if transcript:
                _write_transcript_file(episode["id"], transcript, fmt, output_dir)
                exported += 1
        console.print(
            f"[green]✓ Exported {exported} transcripts to {output_dir}[/green]"
        )
        return

    episode_id = click.prompt("Episode ID to export", type=int)
    transcript = db.get_transcript_by_episode_id(episode_id)
    if not transcript:
        console.print(f"[red]No transcript found for episode {episode_id}[/red]")
        return

    file_path = _write_transcript_file(episode_id, transcript, fmt, output_dir)
    console.print(f"[green]✓ Transcript exported: {file_path}[/green]")


@main.command()
@click.pass_context
def status(ctx):
    """Show processing status of episodes."""
    db = ctx.obj["db"]

    statuses = ["downloaded", "transcribed", "processed"]
    counts = {}

    for s in statuses:
        episodes = db.get_episodes_by_status(s)
        counts[s] = len(episodes)

    table = Table(title="Episode Processing Status")
    table.add_column("Status", style="cyan")
    table.add_column("Count", style="green", justify="right")

    for s, count in counts.items():
        table.add_row(s.title(), str(count))

    console.print(table)


@main.command()
@click.option("--topic", required=True, help="Blog post topic/angle")
@click.option("--date", help="Date to use for digest (YYYY-MM-DD), defaults to today")
@click.option(
    "--target-grade",
    default=91.0,
    help="Target grade for AP English teacher (default: 91.0)",
)
@click.option(
    "--dry-run", is_flag=True, help="Show available summaries without generating"
)
@click.pass_context
def write(ctx, topic, date, target_grade, dry_run):
    """Generate a blog post from a day's digest with an iterative grading loop.

    All summaries from the target date are used as source material. Each
    draft is scored by a strict evaluator persona and revised until it
    reaches the target grade or the iteration limit.
    """
    config = load_config(ctx.obj["config_path"])
    db = ctx.obj["db"]

    settings = config.get("settings", {})
    llm_provider, llm_model = resolve_provider_and_model(settings)

    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            return
    else:
        target_date = datetime.now()

    summaries = db.get_summaries_by_date(target_date)

    if not summaries:
        console.print(f"[yellow]No summaries found for {target_date.date()}[/yellow]")
        console.print("Run 'p3 digest' first to generate summaries")
        return

    if dry_run:
        console.print(
            f"[blue]Dry run — {len(summaries)} summaries available for {target_date.date()}:[/blue]"
        )
        table = Table(title="Available Summaries")
        table.add_column("Podcast", style="cyan")
        table.add_column("Episode", style="white")
        table.add_column("Topics", style="green")
        for s in summaries:
            table.add_row(
                s["podcast_title"],
                s["episode_title"],
                ", ".join(s.get("key_topics", [])[:3]),
            )
        console.print(table)
        return

    writer = BlogWriter(
        db=db,
        llm_provider=llm_provider,
        llm_model=llm_model,
        target_grade=target_grade,
        ollama_base_url=settings.get("ollama_base_url", DEFAULT_OLLAMA_URL),
    )

    console.print(f"[blue]Generating blog post: '{topic}'[/blue]")
    console.print(f"Using {len(summaries)} podcast summaries from {target_date.date()}")
    console.print(f"Target grade: {target_grade}/100")

    with console.status("[bold green]Writing and grading blog post..."):
        blog_result = writer.generate_blog_post_from_digest(topic, summaries)

    console.print("\n[green]Blog post generated![/green]")
    console.print(
        f"Final Grade: {blog_result['final_grade']} ({blog_result['final_score']}/100)"
    )
    console.print(f"Iterations: {len(blog_result['iterations'])}")

    file_path = writer.save_blog_post(blog_result)
    console.print(f"Saved to: {file_path}")

    console.print("\n[blue]Generating social media posts...[/blue]")
    social_posts = writer.generate_social_posts(blog_result)

    if social_posts["twitter"]:
        console.print("\n[cyan]Twitter Posts:[/cyan]")
        for i, post in enumerate(social_posts["twitter"], 1):
            console.print(f"{i}. {post}")

    if social_posts["linkedin"]:
        console.print("\n[cyan]LinkedIn Posts:[/cyan]")
        for i, post in enumerate(social_posts["linkedin"], 1):
            console.print(f"{i}. {post[:100]}...")

    console.print("\n[cyan]Blog Post Preview:[/cyan]")
    console.print("-" * 50)
    preview = blog_result["final_post"][:500]
    console.print(f"{preview}...")
    console.print("-" * 50)
    console.print(f"[green]Complete post saved to: {file_path}[/green]")


@main.command()
@click.pass_context
def init(ctx):
    """Initialize P³ configuration and directories."""
    console.print("[blue]Initializing P³...[/blue]")

    # Check prerequisites
    prereqs = [
        ("ffmpeg", ["ffmpeg", "-version"]),
        ("ollama", ["ollama", "--version"]),
        # yt-dlp's default JavaScript runtime, needed for YouTube sources.
        ("deno", ["deno", "--version"]),
    ]

    missing = []
    for name, cmd in prereqs:
        if _check_prerequisite(name, cmd):
            console.print(f"[green]  {name} found[/green]")
        else:
            missing.append(name)
            console.print(f"[yellow]  {name} not found[/yellow]")

    if missing:
        console.print(
            f"\n[yellow]Warning: missing prerequisites: {', '.join(missing)}[/yellow]"
        )
        console.print("Some pipeline stages may not work without them.")
        if "ffmpeg" in missing:
            console.print("  Install ffmpeg: brew install ffmpeg")
        if "ollama" in missing:
            console.print("  Install Ollama: https://ollama.com")
        if "deno" in missing:
            console.print("  Install Deno (YouTube sources only): brew install deno")
        console.print()

    # Create directories
    dirs = ["data", "config", "data/audio", "exports", "blog_posts", "linkedin_posts"]
    for dir_name in dirs:
        Path(dir_name).mkdir(parents=True, exist_ok=True)
        console.print(f"  Created directory: {dir_name}")

    # Copy example config if it doesn't exist
    config_path = Path(ctx.obj["config_path"])
    example_path = Path("config/feeds.yaml.example")

    if not config_path.exists() and example_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(example_path, config_path)
        console.print(f"  Created {config_path} from example")

    # The database schema is created when the connection is opened in main().
    console.print(f"  Initialized database: {ctx.obj['db_path']}")

    console.print("[green]P³ initialized successfully![/green]")
    console.print("Next steps:")
    console.print(f"1. Edit {config_path} with your RSS feeds")
    console.print("2. Run 'p3 fetch' to download episodes")
    console.print("3. Run 'p3 transcribe' to transcribe audio")
    console.print("4. Run 'p3 digest' to generate summaries")
    console.print("5. Run 'p3 write --topic \"Your Topic\"' to generate blog posts")


if __name__ == "__main__":
    main()
