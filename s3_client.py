"""通用 S3 兼容对象存储客户端 —— 腾讯云 COS / 阿里云 OSS / Cloudflare R2 均适用。

环境变量：
- S3_ENDPOINT_URL      服务端点，如 https://cos.ap-guangzhou.myqcloud.com
- S3_REGION            地域，如 ap-guangzhou
- S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_BUCKET_NAME
- S3_PUBLIC_BASE_URL   可选。公有读桶的固定访问域名
                       （如 https://bucket.cos.ap-guangzhou.myqcloud.com），
                       配置后视频/文件走固定公开地址，可被浏览器缓存；
                       不配置则动态签发预签名地址。

后端只负责：签发预签名 PUT（浏览器直传）、按需签发读取地址、head_object 核实。
数据库只保存 Object Key，不保存任何带签名参数的临时 URL。
"""
import os
from typing import Optional
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PRESIGN_PUT_EXPIRES = 900  # 上传地址 15 分钟有效
PRESIGN_GET_EXPIRES = 6 * 3600  # 访客文件地址 6 小时有效

_s3 = None


def _get_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
            region_name=os.getenv("S3_REGION") or "us-east-1",
            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
            config=Config(signature_version="s3v4"),
        )
    return _s3


def _bucket() -> str:
    return os.getenv("S3_BUCKET_NAME", "")


def s3_configured() -> bool:
    return all(
        os.getenv(k)
        for k in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET_NAME")
    )


def public_url(key: str) -> Optional[str]:
    """配置了公有读域名时返回固定公开地址，否则 None（走预签名）。"""
    base = os.getenv("S3_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/{key}" if base else None


def presign_put_url(key: str, content_type: str) -> str:
    """签发浏览器直传用的 PUT 地址。"""
    return _get_client().generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": key, "ContentType": content_type},
        ExpiresIn=PRESIGN_PUT_EXPIRES,
    )


def presign_get_url(key: str, download_name: Optional[str] = None) -> str:
    """签发读取地址。给 download_name 时强制下载，否则内联（视频播放/PDF 预览）。"""
    url = public_url(key)
    if url and not download_name:
        return url
    params = {"Bucket": _bucket(), "Key": key}
    if download_name:
        params["ResponseContentDisposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"
    return _get_client().generate_presigned_url("get_object", Params=params, ExpiresIn=PRESIGN_GET_EXPIRES)


def head_object(key: str) -> Optional[dict]:
    """校验对象是否存在，返回大小和类型；不存在返回 None。"""
    try:
        resp = _get_client().head_object(Bucket=_bucket(), Key=key)
    except ClientError:
        return None
    return {
        "size": resp.get("ContentLength", 0),
        "content_type": resp.get("ContentType", "application/octet-stream"),
    }


def delete_object(key: str) -> None:
    try:
        _get_client().delete_object(Bucket=_bucket(), Key=key)
    except ClientError:
        pass  # 删除失败不阻塞流程，孤儿文件可后续在控制台清理
