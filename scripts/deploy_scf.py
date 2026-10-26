# -*- coding: utf-8 -*-
"""构建并部署腾讯云 SCF Web函数（项目展示平台）。

用法（在项目目录执行）：
    python scripts/deploy_scf.py

前置条件：
- 本机已安装 Python（py -m pip install tencentcloud-sdk-python-scf boto3）
- 项目 .env 含 S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY / S3_BUCKET_NAME / S3_REGION / S3_ENDPOINT_URL
- SCF 函数已创建（project-platform / ap-guangzhou / Web函数 / Python 3.9）

SCF 运行时两个必知的坑（打包时已处理）：
1. Python 3.9 运行时中 anyio 需要 exceptiongroup，本机 Python>=3.11 时 pip 不会自动装，必须显式加
2. SCF 的 ssl 模块基于 OpenSSL 1.0.2k，urllib3 v2 拒绝启动，必须固定 urllib3<2
"""
import os
import shutil
import subprocess
import sys
import time
import zipfile

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "scf_pkg_build")
PKG = os.path.join(TMP, "pkg")
ZIP = os.path.join(TMP, "scf_pkg.zip")

FUNCTION_NAME = "project-platform"
REGION = "ap-guangzhou"
NAMESPACE = "default"
COS_KEY = "/deploy/scf_pkg.zip"

# SCF Python 3.9 运行时的兼容性依赖
EXTRA_DEPS = ["exceptiongroup>=1.2", "urllib3<2"]


def read_env():
    env = {}
    with open(os.path.join(PROJ, ".env"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"')
    return env


def build():
    if os.path.exists(PKG):
        shutil.rmtree(PKG)
    os.makedirs(PKG)
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--quiet",
        "--platform", "manylinux2014_x86_64",
        "--python-version", "3.9",
        "--implementation", "cp",
        "--abi", "cp39",
        "--only-binary=:all:",
        "-t", PKG,
        "-r", os.path.join(PROJ, "requirements.txt"),
        *EXTRA_DEPS,
    ])
    for name in ["main.py", "database.py", "s3_client.py", "templates", "static", "scf_bootstrap"]:
        src = os.path.join(PROJ, name)
        dst = os.path.join(PKG, name)
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    if os.path.exists(ZIP):
        os.remove(ZIP)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(PKG):
            rel_dir = os.path.relpath(root, PKG).replace("\\", "/")
            if rel_dir != ".":
                zi = zipfile.ZipInfo(rel_dir + "/")
                zi.external_attr = 0o755 << 16
                zf.writestr(zi, b"")
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, PKG).replace("\\", "/")
                zi = zipfile.ZipInfo(rel)
                zi.external_attr = (0o755 if rel == "scf_bootstrap" else 0o644) << 16
                with open(full, "rb") as fh:
                    zf.writestr(zi, fh.read())
    print(f"构建完成: {ZIP}（{os.path.getsize(ZIP)/1024/1024:.1f} MB）")


def upload_cos(env):
    import boto3
    from botocore.config import Config
    s3 = boto3.client(
        "s3",
        aws_access_key_id=env["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=env["S3_SECRET_ACCESS_KEY"],
        region_name=env["S3_REGION"],
        endpoint_url=env["S3_ENDPOINT_URL"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    with open(ZIP, "rb") as fh:
        s3.put_object(Bucket=env["S3_BUCKET_NAME"], Key=COS_KEY.lstrip("/"),
                      Body=fh, ContentType="application/zip")
    print(f"已上传 COS {COS_KEY}")


def update_function(env):
    from tencentcloud.common import credential
    from tencentcloud.scf.v20180416 import models, scf_client
    cred = credential.Credential(env["S3_ACCESS_KEY_ID"], env["S3_SECRET_ACCESS_KEY"])
    client = scf_client.ScfClient(cred, REGION)

    req = models.UpdateFunctionCodeRequest()
    req.FunctionName = FUNCTION_NAME
    req.Namespace = NAMESPACE
    req.CosBucketName = env["S3_BUCKET_NAME"]
    req.CosObjectName = COS_KEY
    client.UpdateFunctionCode(req)
    print("UpdateFunctionCode 已提交，等待部署...")

    for i in range(24):
        time.sleep(5)
        g = models.GetFunctionRequest()
        g.FunctionName = FUNCTION_NAME
        g.Namespace = NAMESPACE
        info = client.GetFunction(g)
        print(f"[{i*5+5}s] Status: {info.Status}")
        if info.Status == "Active":
            print("部署成功")
            return
        if info.Status not in ("Updating", "Creating"):
            raise SystemExit(f"部署异常: {info.Status} {info.StatusReason}")
    raise SystemExit("120 秒内未完成，请到控制台检查")


if __name__ == "__main__":
    env = read_env()
    build()
    upload_cos(env)
    update_function(env)
