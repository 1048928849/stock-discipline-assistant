import asyncio
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings


class XUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectedPost:
    post_id: str
    author: str
    content: str
    published_at: datetime
    metrics: dict
    url: str


class TWScrapeProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.x_cookie)

    async def _collect(self, queries: list[str], limit: int) -> list[CollectedPost]:
        if not self.configured:
            raise XUnavailableError("X Cookie 未配置，采集任务保持暂停")
        database_path = ""
        try:
            from twscrape import API

            file_descriptor, database_path = tempfile.mkstemp(prefix="stock-x-", suffix=".db")
            os.close(file_descriptor)
            api = API(database_path, raise_when_no_account=True, wait_timeout=10)
            await api.pool.add_account(
                "local_cookie_user", "", "", "", cookies=self.settings.x_cookie
            )
            posts: dict[str, CollectedPost] = {}
            for query in queries:
                async for tweet in api.search(query, limit=limit):
                    posts[tweet.id_str] = CollectedPost(
                        post_id=tweet.id_str,
                        author=tweet.user.username,
                        content=tweet.rawContent,
                        published_at=tweet.date.replace(tzinfo=None),
                        metrics={
                            "reply": tweet.replyCount,
                            "retweet": tweet.retweetCount,
                            "like": tweet.likeCount,
                            "quote": tweet.quoteCount,
                            "view": tweet.viewCount,
                        },
                        url=tweet.url,
                    )
            return list(posts.values())
        except XUnavailableError:
            raise
        except Exception as exc:
            raise XUnavailableError(f"X 采集失败或凭据受限：{type(exc).__name__}") from exc
        finally:
            if database_path:
                try:
                    os.unlink(database_path)
                except OSError:
                    pass

    def collect(self, queries: list[str], limit: int = 20) -> list[CollectedPost]:
        return asyncio.run(self._collect(queries, limit))
