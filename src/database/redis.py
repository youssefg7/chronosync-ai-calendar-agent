"""Implementation of a langgraph async checkpoint saver using Redis."""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (BaseCheckpointSaver, ChannelVersions,
                                       Checkpoint, CheckpointMetadata,
                                       CheckpointTuple, PendingWrite,
                                       get_checkpoint_id)
from langgraph.checkpoint.serde.base import SerializerProtocol
from redis.asyncio import Redis as AsyncRedis

from helpers import get_settings

REDIS_KEY_SEPARATOR = ":"

app_settings = get_settings()

# Utilities shared by both RedisSaver and AsyncRedisSaver


def _make_redis_checkpoint_key(
    thread_id: str, checkpoint_ns: str, checkpoint_id: str
) -> str:
    return REDIS_KEY_SEPARATOR.join(
        ["checkpoint", thread_id, checkpoint_ns, checkpoint_id]
    )


def _make_redis_checkpoint_writes_key(
    thread_id: str,
    checkpoint_ns: str,
    checkpoint_id: str,
    task_id: str,
    idx: Optional[int],
) -> str:
    if idx is None:
        return REDIS_KEY_SEPARATOR.join(
            ["writes", thread_id, checkpoint_ns, checkpoint_id, task_id]
        )

    return REDIS_KEY_SEPARATOR.join(
        ["writes", thread_id, checkpoint_ns, checkpoint_id, task_id, str(idx)]
    )


def _parse_redis_checkpoint_key(redis_key: str) -> dict:
    parts = redis_key.split(REDIS_KEY_SEPARATOR)
    if parts[0] != "checkpoint":
        raise ValueError("Expected checkpoint key to start with 'checkpoint'")
    thread_id = parts[1]
    checkpoint_id = parts[-1]
    checkpoint_ns = REDIS_KEY_SEPARATOR.join(parts[2:-1])
    return {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "checkpoint_id": checkpoint_id,
    }


def _parse_redis_checkpoint_writes_key(redis_key: str) -> dict:
    parts = redis_key.split(REDIS_KEY_SEPARATOR)
    if parts[0] != "writes":
        raise ValueError("Expected writes key to start with 'writes'")
    thread_id = parts[1]
    idx = parts[-1]
    task_id = parts[-2]
    checkpoint_id = parts[-3]
    checkpoint_ns = REDIS_KEY_SEPARATOR.join(parts[2:-3])
    return {
        "thread_id": thread_id,
        "checkpoint_ns": checkpoint_ns,
        "checkpoint_id": checkpoint_id,
        "task_id": task_id,
        "idx": idx,
    }


def _filter_keys(
    keys: List[str], before: Optional[RunnableConfig], limit: Optional[int]
) -> list:
    """Filter and sort Redis keys based on optional criteria."""
    if before:
        keys = [
            k
            for k in keys
            if _parse_redis_checkpoint_key(k.decode())["checkpoint_id"]
            < before["configurable"]["checkpoint_id"]
        ]

    keys = sorted(
        keys,
        key=lambda k: _parse_redis_checkpoint_key(k.decode())["checkpoint_id"],
        reverse=True,
    )
    if limit:
        keys = keys[:limit]
    return keys


def _dump_writes(serde: SerializerProtocol, writes: tuple[str, Any]) -> list[dict]:
    """Serialize pending writes."""
    serialized_writes = []
    for channel, value in writes:
        type_, serialized_value = serde.dumps_typed(value)
        serialized_writes.append(
            {"channel": channel, "type": type_, "value": serialized_value}
        )
    return serialized_writes


def _load_writes(
    serde: SerializerProtocol, task_id_to_data: dict[tuple[str, str], dict]
) -> list[PendingWrite]:
    """Deserialize pending writes."""
    writes = [
        (
            task_id,
            data[b"channel"].decode(),
            serde.loads_typed((data[b"type"].decode(), data[b"value"])),
        )
        for (task_id, _), data in task_id_to_data.items()
    ]
    return writes


def _parse_redis_checkpoint_data(
    serde: SerializerProtocol,
    key: str,
    data: dict,
    pending_writes: Optional[List[PendingWrite]] = None,
) -> Optional[CheckpointTuple]:
    """Parse checkpoint data retrieved from Redis."""
    if not data:
        return None

    parsed_key = _parse_redis_checkpoint_key(key)
    thread_id = parsed_key["thread_id"]
    checkpoint_ns = parsed_key["checkpoint_ns"]
    checkpoint_id = parsed_key["checkpoint_id"]
    config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": checkpoint_id,
        }
    }

    checkpoint = serde.loads_typed((data[b"type"].decode(), data[b"checkpoint"]))
    metadata = serde.loads(data[b"metadata"].decode())
    parent_checkpoint_id = data.get(b"parent_checkpoint_id", b"").decode()
    parent_config = (
        {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": parent_checkpoint_id,
            }
        }
        if parent_checkpoint_id
        else None
    )
    return CheckpointTuple(
        config=config,
        checkpoint=checkpoint,
        metadata=metadata,
        parent_config=parent_config,
        pending_writes=pending_writes,
    )


class AsyncRedisSaver(BaseCheckpointSaver):
    """Async redis-based checkpoint saver implementation."""

    conn: AsyncRedis

    def __init__(self, conn: AsyncRedis):
        super().__init__()
        self.conn = conn

    @classmethod
    @asynccontextmanager
    async def from_conn_info(
        cls, *, host: str, port: int, db: int, password: str = None
    ) -> AsyncIterator["AsyncRedisSaver"]:
        conn = None
        try:
            conn = AsyncRedis(host=host, port=port, db=db, password=password)
            yield AsyncRedisSaver(conn)
        finally:
            if conn:
                await conn.aclose()

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Save a checkpoint to the database asynchronously.

        This method saves a checkpoint to Redis. The checkpoint is associated
        with the provided config and its parent config (if any).

        Args:
            config (RunnableConfig): The config to associate with the checkpoint.
            checkpoint (Checkpoint): The checkpoint to save.
            metadata (CheckpointMetadata): Additional metadata to save with the checkpoint.
            new_versions (ChannelVersions): New channel versions as of this write.

        Returns:
            RunnableConfig: Updated configuration after storing the checkpoint.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]
        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        key = _make_redis_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)

        type_, serialized_checkpoint = self.serde.dumps_typed(checkpoint)
        serialized_metadata = self.serde.dumps(metadata)
        data = {
            "checkpoint": serialized_checkpoint,
            "type": type_,
            "checkpoint_id": checkpoint_id,
            "metadata": serialized_metadata,
            "parent_checkpoint_id": (
                parent_checkpoint_id if parent_checkpoint_id else ""
            ),
        }

        await self.conn.hset(key, mapping=data)
        await self.conn.expire(key, app_settings.REDIS_TTL_SECONDS)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: List[Tuple[str, Any]],
        task_id: str,
    ) -> RunnableConfig:
        """Store intermediate writes linked to a checkpoint asynchronously.

        This method saves intermediate writes associated with a checkpoint to the database.

        Args:
            config (RunnableConfig): Configuration of the related checkpoint.
            writes (Sequence[Tuple[str, Any]]): List of writes to store, each as (channel, value) pair.
            task_id (str): Identifier for the task creating the writes.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = config["configurable"]["checkpoint_id"]

        for idx, data in enumerate(_dump_writes(self.serde, writes)):
            key = _make_redis_checkpoint_writes_key(
                thread_id, checkpoint_ns, checkpoint_id, task_id, idx
            )
            await self.conn.hset(key, mapping=data)
            await self.conn.expire(key, app_settings.REDIS_TTL_SECONDS)
        return config

    async def aget_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Get a checkpoint tuple from Redis asynchronously.

        This method retrieves a checkpoint tuple from Redis based on the
        provided config. If the config contains a "checkpoint_id" key, the checkpoint with
        the matching thread ID and checkpoint ID is retrieved. Otherwise, the latest checkpoint
        for the given thread ID is retrieved.

        Args:
            config (RunnableConfig): The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_id = get_checkpoint_id(config)
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")

        checkpoint_key = await self._aget_checkpoint_key(
            self.conn, thread_id, checkpoint_ns, checkpoint_id
        )
        if not checkpoint_key:
            return None
        checkpoint_data = await self.conn.hgetall(checkpoint_key)

        # load pending writes
        checkpoint_id = (
            checkpoint_id
            or _parse_redis_checkpoint_key(checkpoint_key)["checkpoint_id"]
        )
        writes_key = _make_redis_checkpoint_writes_key(
            thread_id, checkpoint_ns, checkpoint_id, "*", None
        )
        matching_keys = await self.conn.keys(pattern=writes_key)
        parsed_keys = [
            _parse_redis_checkpoint_writes_key(key.decode()) for key in matching_keys
        ]
        pending_writes = _load_writes(
            self.serde,
            {
                (parsed_key["task_id"], parsed_key["idx"]): await self.conn.hgetall(key)
                for key, parsed_key in sorted(
                    zip(matching_keys, parsed_keys), key=lambda x: x[1]["idx"]
                )
            },
        )
        return _parse_redis_checkpoint_data(
            self.serde, checkpoint_key, checkpoint_data, pending_writes=pending_writes
        )

    async def alist(
        self,
        config: Optional[RunnableConfig],
        *,
        # TODO: implement filtering
        filter: Optional[dict[str, Any]] = None,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> AsyncGenerator[CheckpointTuple, None]:
        """List checkpoints from Redis asynchronously.

        This method retrieves a list of checkpoint tuples from Redis based
        on the provided config. The checkpoints are ordered by checkpoint ID in descending order (newest first).

        Args:
            config (Optional[RunnableConfig]): Base configuration for filtering checkpoints.
            filter (Optional[Dict[str, Any]]): Additional filtering criteria for metadata.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified checkpoint ID are returned. Defaults to None.
            limit (Optional[int]): Maximum number of checkpoints to return.

        Yields:
            AsyncIterator[CheckpointTuple]: An asynchronous iterator of matching checkpoint tuples.
        """
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        pattern = _make_redis_checkpoint_key(thread_id, checkpoint_ns, "*")
        keys = _filter_keys(await self.conn.keys(pattern), before, limit)
        for key in keys:
            data = await self.conn.hgetall(key)
            if data and b"checkpoint" in data and b"metadata" in data:
                yield _parse_redis_checkpoint_data(self.serde, key.decode(), data)

    async def _aget_checkpoint_key(
        self, conn, thread_id: str, checkpoint_ns: str, checkpoint_id: Optional[str]
    ) -> Optional[str]:
        """Asynchronously determine the Redis key for a checkpoint."""
        if checkpoint_id:
            return _make_redis_checkpoint_key(thread_id, checkpoint_ns, checkpoint_id)

        all_keys = await conn.keys(
            _make_redis_checkpoint_key(thread_id, checkpoint_ns, "*")
        )
        if not all_keys:
            return None

        latest_key = max(
            all_keys,
            key=lambda k: _parse_redis_checkpoint_key(k.decode())["checkpoint_id"],
        )
        return latest_key.decode()


async def get_redis_saver():
    async with AsyncRedisSaver.from_conn_info(
        host=app_settings.REDIS_HOST,
        port=app_settings.REDIS_PORT,
        db=app_settings.REDIS_DB,
        password=app_settings.REDIS_PASSWORD,
    ) as checkpointer:
        yield checkpointer


class AsyncRedisManager(AsyncRedisSaver):

    @classmethod
    @asynccontextmanager
    async def from_conn_info(
        cls, *, host: str, port: int, db: int, password: str = None
    ) -> AsyncIterator["AsyncRedisManager"]:
        conn = None
        try:
            conn = AsyncRedis(host=host, port=port, db=db, password=password)
            yield AsyncRedisManager(conn)
        finally:
            if conn:
                await conn.aclose()


async def get_redis_manager():
    async with AsyncRedisManager.from_conn_info(
        host=app_settings.REDIS_HOST,
        port=app_settings.REDIS_PORT,
        db=app_settings.REDIS_DB,
        password=app_settings.REDIS_PASSWORD,
    ) as manager:
        yield manager
