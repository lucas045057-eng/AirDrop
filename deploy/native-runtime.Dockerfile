# 原生运行环境的基础镜像：只提供 Python 运行时与非 root 用户。
# 脚本依赖由控制器在受控构建阶段安装到派生镜像里，基础镜像本身不含任何账号资料。
# 生产部署请固定到镜像摘要，并让 AIRDROP_NATIVE_RUNTIME_BASE_IMAGE 指向该摘要。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TMPDIR=/tmp

# 容器以 nobody(65534) 运行；/work 由控制器在宿主 tmpfs 上准备并挂载。
WORKDIR /work
USER 65534:65534

CMD ["python", "-c", "print('airdrop native runtime base image')"]
