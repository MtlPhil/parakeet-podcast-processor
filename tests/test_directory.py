"""Podcast discovery against stand-in directory answers (no network)."""

import pytest

from p3 import directory


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _item(n, feed=True, genres=("1321", "26")):
    return {
        "collectionId": n,
        "collectionName": f"Show {n}",
        "artistName": "Host",
        "feedUrl": f"https://feeds.example.com/{n}" if feed else None,
        "artworkUrl100": f"https://img.example.com/{n}.jpg",
        "primaryGenreName": "Business",
        "genreIds": list(genres),
        "trackCount": 10,
    }


@pytest.fixture
def answers(monkeypatch):
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, params))
        if "/rss/toppodcasts/" in url:
            return FakeResponse(
                {
                    "feed": {
                        "entry": [
                            {"id": {"attributes": {"im:id": "3"}}},
                            {"id": {"attributes": {"im:id": "1"}}},
                            {"id": {"attributes": {"im:id": "2"}}},
                        ]
                    }
                }
            )
        if "lookup" in url:
            return FakeResponse({"results": [_item(1), _item(2, feed=False), _item(3)]})
        return FakeResponse(
            {"results": [_item(1), _item(2, feed=False), _item(4, genres=("1318",))]}
        )

    monkeypatch.setattr(directory.requests, "get", get)
    return calls


def test_search_drops_shows_without_a_feed_and_filters_by_category(answers):
    assert [s["name"] for s in directory.search("money")] == ["Show 1", "Show 4"]
    assert [s["name"] for s in directory.search("money", genre=1321)] == ["Show 1"]
    assert answers[0][1]["term"] == "money" and answers[0][1]["country"] == "us"


def test_top_keeps_the_chart_order(answers):
    shows = directory.top(1321, "CA")
    assert [s["feed_url"] for s in shows] == [
        "https://feeds.example.com/3",
        "https://feeds.example.com/1",
    ]
    assert "/ca/rss/toppodcasts/" in answers[0][0]
    assert answers[1][1]["id"] == "3,1,2"


def test_a_bad_country_falls_back_and_errors_are_named(monkeypatch):
    assert directory._country("../x") == "us"

    def down(*a, **k):
        raise directory.requests.ConnectionError("no route")

    monkeypatch.setattr(directory.requests, "get", down)
    with pytest.raises(directory.DirectoryError):
        directory.search("x")
