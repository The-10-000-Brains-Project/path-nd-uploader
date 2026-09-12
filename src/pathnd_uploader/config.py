"""GCS client/bucket resolution.

Deliberately does not implement any custom auth — relies entirely on
Application Default Credentials (`gcloud auth application-default login`
for interactive use, or `GOOGLE_APPLICATION_CREDENTIALS` pointing at a
service-account key for automated/batch use).
"""

from __future__ import annotations

from google.cloud import storage


def get_bucket(bucket_name: str, *, project: str | None = None) -> storage.Bucket:
    client = storage.Client(project=project)
    return client.bucket(bucket_name)
