import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings


class StorageService:
    def __init__(self):
        """
        Filebase is a public HTTPS endpoint, so the same client works for
        the backend AND for generating URLs the browser can use.
        No internal/public endpoint split (and no URL replacing) is needed.
        """
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,  # https://s3.filebase.com
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            region_name=settings.S3_REGION,  # us-east-1
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
            ),
        )
        self.bucket_name = settings.S3_BUCKET_NAME

    async def generate_presigned_put_url(
        self,
        object_key: str,
        expiration: int = 3600,
        content_type: str = "video/mp4",
    ) -> str:
        # The browser must send the SAME Content-Type header when uploading
        return self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": object_key,
                "ContentType": content_type,
            },
            ExpiresIn=expiration,
        )

    async def object_exists(self, object_key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=object_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise  # auth/network problems should not look like "missing file"

    def download_file(self, object_key: str, local_path: str):
        self.client.download_file(self.bucket_name, object_key, local_path)

    def upload_file(self, local_path: str, object_key: str, content_type: str = None):
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        self.client.upload_file(
            local_path, self.bucket_name, object_key, ExtraArgs=extra_args
        )

    def generate_presigned_get_url(self, object_key: str, expiration: int = 60) -> str:
        """Short-lived signed URL; required because the bucket is private."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket_name, "Key": object_key},
            ExpiresIn=expiration,
        )

    def get_public_url(self, object_key: str) -> str:
        """
        A plain public URL cannot work on a private bucket (it returns 403),
        so this returns a presigned URL instead.
        """
        return self.generate_presigned_get_url(object_key, expiration=3600)

    def delete_object(self, object_key: str) -> bool:
        """Delete a single object from S3. Returns True if deleted, False if not found."""
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=object_key)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def delete_objects(self, object_keys: list[str]) -> dict[str, bool]:
        """Delete multiple objects from S3. Returns dict of key -> success."""
        if not object_keys:
            return {}
        try:
            response = self.client.delete_objects(
                Bucket=self.bucket_name,
                Delete={"Objects": [{"Key": k} for k in object_keys]}
            )
            deleted = {obj["Key"]: True for obj in response.get("Deleted", [])}
            errors = {obj["Key"]: False for obj in response.get("Errors", [])}
            return {**deleted, **errors}
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return {k: False for k in object_keys}
            raise

    def delete_objects_by_prefix(self, prefix: str) -> int:
        """Delete all objects with a given prefix. Returns count of deleted objects."""
        deleted_count = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            if "Contents" not in page:
                continue
            keys = [obj["Key"] for obj in page["Contents"]]
            if keys:
                result = self.delete_objects(keys)
                deleted_count += sum(1 for v in result.values() if v)
        return deleted_count


storage_service = StorageService()