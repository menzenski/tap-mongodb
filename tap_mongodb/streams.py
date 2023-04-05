"""mongodb streams class."""

from __future__ import annotations

from os import PathLike
from typing import Iterable, Any
from datetime import datetime

from singer_sdk import PluginBase as TapBaseClass, _singerlib as singer
from singer_sdk.streams import Stream
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo.collection import Collection
from pymongo import ASCENDING
from singer_sdk.streams.core import (
    TypeConformanceLevel,
    REPLICATION_LOG_BASED,
    REPLICATION_INCREMENTAL,
)
from singer_sdk.helpers._state import increment_state
from singer_sdk._singerlib.utils import strptime_to_utc


class CollectionStream(Stream):
    """Stream class for mongodb streams."""

    # The output stream will always have _id as the primary key
    primary_keys = ["_id"]
    replication_key = "_id"

    # Disable timestamp replication keys. One caveat is this relies on an
    # alphanumerically sortable replication key. Python __gt__ and __lt__ are
    # used to compare the replication key values. This works for most cases.
    is_timestamp_replication_key = False

    # No conformance level is set by default since this is a generic stream
    TYPE_CONFORMANCE_LEVEL = TypeConformanceLevel.NONE

    def __init__(
        self,
        tap: TapBaseClass,
        schema: str | PathLike | dict[str, Any] | singer.Schema | None = None,
        name: str | None = None,
        collection: Collection | None = None,
        max_await_time_ms: int | None = None,
    ) -> None:
        super().__init__(tap, schema, name)
        self._collection: Collection = collection
        self._max_await_time_ms = max_await_time_ms

    def _increment_stream_state(
        self, latest_record: dict[str, Any], *, context: dict | None = None
    ) -> None:
        """Update state of stream or partition with data from the provided record.

        Raises `InvalidStreamSortException` is `self.is_sorted = True` and unsorted data
        is detected.

        Note: The default implementation does not advance any bookmarks unless
        `self.replication_method == 'INCREMENTAL'.

        Args:
            latest_record: TODO
            context: Stream partition or context dictionary.

        Raises:
            ValueError: TODO
        """
        # This also creates a state entry if one does not yet exist:
        state_dict = self.get_context_state(context)

        # Advance state bookmark values if applicable
        if not self.replication_key:
            raise ValueError(
                f"Could not detect replication key for '{self.name}' stream"
                f"(replication method={self.replication_method})",
            )
        treat_as_sorted = self.is_sorted
        if not treat_as_sorted and self.state_partitioning_keys is not None:
            # Streams with custom state partitioning are not resumable.
            treat_as_sorted = False
        increment_state(
            state_dict,
            replication_key=self.replication_key,
            latest_record=latest_record,
            is_sorted=treat_as_sorted,
            check_sorted=self.check_sorted,
        )

    def get_records(self, context: dict | None) -> Iterable[dict]:
        """Return a generator of record-type dictionary objects."""
        bookmark: str = self.get_starting_replication_key_value(context)
        self.logger.info(f"a: bookmark {bookmark}")
        self.logger.info(f"a: replication_method {self.replication_method}")

        if self.replication_method == REPLICATION_INCREMENTAL:
            start_date: ObjectId | None = None
            if bookmark:
                try:
                    start_date = ObjectId(bookmark)
                except InvalidId:
                    self.logger.warning(
                        f"Replication key value {bookmark} cannot be parsed into ObjectId."
                    )
            else:
                start_date_str = self.config.get("start_date", "1970-01-01")
                self.logger.info(f"using start_date_str: {start_date_str}")
                start_date_dt: datetime = strptime_to_utc(start_date_str)
                start_date = ObjectId.from_datetime(start_date_dt)

            for record in self._collection.find({"_id": {"$gt": start_date}}).sort(
                [("_id", ASCENDING)]
            ):
                object_id: ObjectId = record["_id"]
                yield {"_id": str(object_id), "document": record}

        elif self.replication_method == REPLICATION_LOG_BASED:
            self.logger.info(f"m: bookmark {bookmark}")
            change_stream_options = {"full_document": "updateLookup"}
            self.logger.info(f"m: bookmark {bookmark}")
            self.logger.info(f"m: type(bookmark) {type(bookmark)}")
            if bookmark is not None:
                change_stream_options["resume_after"] = {"_data": bookmark}
            self.logger.info(f"m: change_stream_options {change_stream_options}")
            keep_open: bool = True
            with self._collection.watch(**change_stream_options) as change_stream:
                while change_stream.alive and keep_open:
                    self.logger.info(f"b change_stream.alive: {change_stream.alive}")
                    record = change_stream.try_next()
                    if record is None:
                        self.logger.info(f"b record is None: {record}")
                        self.logger.info(
                            f'b change_stream.resume_token["_data"]: {change_stream.resume_token["_data"]}'
                        )
                        yield {
                            "_id": change_stream.resume_token["_data"],
                            "document": None,
                        }
                        self.logger.info("b after yield, before close")
                        # change_stream.close()
                        keep_open = False
                        self.logger.info("b after yield, after close")
                    if record is not None:
                        self.logger.info(
                            f"b change_stream record is not None: {record}"
                        )
                        yield {
                            "_id": record["_id"]["_data"],
                            "document": record["fullDocument"],
                            "operationType": record["operationType"],
                            "clusterTime": int(
                                record["clusterTime"].as_datetime().timestamp()
                            ),
                            "ns": record["ns"],
                        }

        else:
            msg = f"Unrecognized replication strategy {self.replication_method}"
            self.logger.critical(msg)
            raise RuntimeError(msg)
