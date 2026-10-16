#!/bin/sh
# 原生运行网络的宿主防火墙规则。
# 控制器没有 NET_ADMIN，因此由部署者在目标主机执行本脚本；控制器只会在每次
# 业务容器启动前用探针复核这些地址确实不可达，未通过时拒绝启动。
# DOCKER-USER 只拦截转发流量；INPUT 规则封锁专用网桥到宿主所有地址和端口。
set -eu

NETWORK="${AIRDROP_NATIVE_NETWORK:-airdrop-native}"
BLOCKED="${AIRDROP_NATIVE_BLOCKED_TARGETS:-169.254.169.254 169.254.170.2 100.100.100.200}"
SENSITIVE_CIDRS="${AIRDROP_NATIVE_BLOCKED_CIDRS:-}"

if ! command -v iptables >/dev/null 2>&1; then
  echo "未找到 iptables；请在宿主机上手动配置等价规则" >&2
  exit 1
fi

BRIDGE="br-$(docker network inspect --format '{{.Id}}' "$NETWORK" | cut -c1-12)"
if [ -z "$BRIDGE" ]; then
  echo "请先创建平台专用网络：docker network create --subnet 172.31.240.0/24 $NETWORK" >&2
  exit 1
fi

iptables -N AIRDROP-NATIVE 2>/dev/null || true
iptables -F AIRDROP-NATIVE
for host in $BLOCKED; do
  iptables -A AIRDROP-NATIVE -d "$host" -j DROP
done
for cidr in $SENSITIVE_CIDRS; do
  iptables -A AIRDROP-NATIVE -d "$cidr" -j DROP
done
iptables -C DOCKER-USER -i "$BRIDGE" -j AIRDROP-NATIVE 2>/dev/null || \
  iptables -I DOCKER-USER 1 -i "$BRIDGE" -j AIRDROP-NATIVE

iptables -N AIRDROP-NATIVE-INPUT 2>/dev/null || true
iptables -F AIRDROP-NATIVE-INPUT
iptables -A AIRDROP-NATIVE-INPUT -j DROP
iptables -C INPUT -i "$BRIDGE" -j AIRDROP-NATIVE-INPUT 2>/dev/null || \
  iptables -I INPUT 1 -i "$BRIDGE" -j AIRDROP-NATIVE-INPUT

echo "已为 $NETWORK ($BRIDGE) 应用转发阻断规则：$BLOCKED ${SENSITIVE_CIDRS}"
echo "已阻断该网桥到宿主所有 INPUT 地址与端口的连接"
