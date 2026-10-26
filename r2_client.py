"""Cloudflare R2（S3 兼容）客户端封装 —— 浏览器直传方案。

后端只负责三件事：
1. 签发短期预签名 PUT 地址，浏览器把文件直传 R2，不占用 Render 内存/带宽
2. 访问时动态签发预签名 GET 地址（分享页每次打开都现签，永不失效）
3. head_object 核实文件确实上传成功后才入库

数据库只保存 Object Key；绑定了 R2 自定义域时可改用公开固定地址（可缓存）。
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
        account_id = os.getenv("R2_ACCOUNT_ID")
        _s3 = boto3.client(
            "s3",
            aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"),
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            config=Config(signature_version="s3v4"),
        )
    return _s3


def _bucket() -> str:
    return os.getenv("R2_BUCKET_NAME", "")


def r2_configured() -> bool:
    return all(
        os.getenv(k)
        for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    )


def public_url(key: str) -> Optional[str]:
    """绑定了 R2 自定义域时返回公开固定地址，否则 None（走预签名）。"""
    base = os.getenv("R2_PUBLIC_BASE_URL", "").rstrip("/")
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
        pass  # 删除失败不阻塞流程，孤儿文件可后续在 R2 控制台清理
