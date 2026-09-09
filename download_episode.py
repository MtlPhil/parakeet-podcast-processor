#!/usr/bin/env python3
"""Download a podcast episode from an Apple Podcasts link.

Usage:
    python download_episode.py "https://podcasts.apple.com/us/podcast/.../id990149481?i=1000758401408"

How it works:
    1. Extracts the podcast ID from the Apple Podcasts URL
    2. Looks up the RSS feed via the iTunes API
    3. Parses the RSS feed to find the matching episode
    4. Downloads the audio file
"""

import re
import sys
from pathlib import Path

import feedparser
import requests


def get_rss_from_apple_url(apple_url: str) -> tuple[str, str, str]:
    """Resolve an Apple Podcasts URL to an RSS feed URL.

    Returns (rss_url, podcast_name, episode_id).
    """
    # Extract podcast ID (id\d+) and episode ID (i=\d+)
    pod_match = re.search(r"id(\d+)", apple_url)
    ep_match = re.search(r"[?&]i=(\d+)", apple_url)

    if not pod_match:
        sys.exit("Could not extract podcast ID from URL")

    podcast_id = pod_match.group(1)
    episode_id = ep_match.group(1) if ep_match else None

    # Look up RSS feed via iTunes API
    resp = requests.get(
        f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcast",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    for result in data.get("results", []):
        feed_url = result.get("feedUrl")
        name = result.get("collectionName", "Unknown Podcast")
        if feed_url:
            print(f"Podcast: {name}")
            print(f"RSS feed: {feed_url}")
            return feed_url, name, episode_id

    sys.exit(f"No RSS feed found for podcast ID {podcast_id}")


def find_episode_in_feed(rss_url: str, episode_id: str = None) -> tuple[str, str]:
    """Parse RSS feed and find the episode. Returns (title, audio_url)."""
    feed = feedparser.parse(rss_url)

    for entry in feed.entries:
        # Match by Apple episode ID in the guid or URL if available
        # Otherwise fall back to checking all episodes
        audio_url = None
        for enc in entry.get("enclosures", []):
            if enc.get("type", "").startswith("audio"):
                audio_url = enc.get("href")
                break

        if not audio_url:
            # Some feeds use <media:content> instead
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio"):
                    audio_url = link.get("href")
                    break

        if not audio_url:
            continue

        # If we have an episode ID, try to match it
        if episode_id:
            guid = entry.get("id", "")
            # Apple episode IDs sometimes appear in guid or itunes tags
            if episode_id in guid or episode_id in str(entry):
                return entry.get("title", "Unknown"), audio_url

        # Fallback: match by title keywords from the Apple URL
        title = entry.get("title", "")
        if "future of education" in title.lower() or "alpha school" in title.lower():
            return title, audio_url

    # If no specific match, list available episodes
    print("\nCould not find exact episode. Recent episodes:")
    for i, entry in enumerate(feed.entries[:10]):
        print(f"  {i+1}. {entry.get('title', 'Unknown')}")

    if feed.entries:
        # Download the first/latest episode as fallback
        entry = feed.entries[0]
        for enc in entry.get("enclosures", []):
            if enc.get("type", "").startswith("audio"):
                title = entry.get("title", "Unknown")
                print(f"\nDefaulting to latest: {title}")
                return title, enc.get("href")

    sys.exit("No downloadable episodes found in feed")


def download_audio(audio_url: str, title: str, output_dir: str = "data/audio") -> str:
    """Download the audio file."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Clean title for filename
    safe_title = re.sub(r"[^\w\s-]", "", title)
    safe_title = re.sub(r"\s+", "_", safe_title).strip("_")[:60]

    # Guess extension from URL
    ext = "mp3"
    if ".m4a" in audio_url:
        ext = "m4a"
    elif ".wav" in audio_url:
        ext = "wav"

    output_path = Path(output_dir) / f"{safe_title}.{ext}"

    print(f"\nDownloading: {title}")
    print(f"From: {audio_url}")
    print(f"To: {output_path}")

    resp = requests.get(audio_url, stream=True, timeout=300)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded / total * 100
                print(f"\r  {pct:.1f}% ({downloaded // 1024 // 1024}MB / {total // 1024 // 1024}MB)", end="")

    print(f"\n\nSaved to: {output_path}")
    return str(output_path)


def main():
    if len(sys.argv) < 2:
        print("Usage: python download_episode.py <apple-podcasts-url>")
        print('Example: python download_episode.py "https://podcasts.apple.com/us/podcast/top-principal/id990149481?i=1000758401408"')
        sys.exit(1)

    apple_url = sys.argv[1]
    print(f"Resolving: {apple_url}\n")

    rss_url, podcast_name, episode_id = get_rss_from_apple_url(apple_url)
    title, audio_url = find_episode_in_feed(rss_url, episode_id)
    download_audio(audio_url, title)


if __name__ == "__main__":
    main()
