"""增量日志脱敏。

控制器按到达顺序处理容器输出：先做增量 UTF-8 解码，再按已注入凭据精确遮蔽
并保留跨块匹配窗口，最后按常见授权格式做保守遮蔽。"完整日志"指敏感值被替换
为遮蔽标记的完整 stdout/stderr，不承诺识别脚本刻意编码或拆散的所有秘密。
"""
import codecs
import re

MASK = '[REDACTED]'
MIN_SECRET = 6
# 即使没有已注入凭据也要保留窗口，避免长授权串跨块漏检。
MIN_WINDOW = 128
PATTERNS = (
    re.compile(r'(?i)\b(bearer|token|api[_-]?key|apikey|secret|password|passwd|private[_-]?key)\b(\s*[:=]\s*)(\S+)'),
    re.compile(r'(?i)\b(authorization|cookie|set-cookie)\s*:\s*([^\r\n]+)'),
    re.compile(r'eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}'),
    re.compile(r'\b(?:sk|pk|ghp|xox[baprs])[-_][A-Za-z0-9_\-]{16,}'),
)


class Redactor:
    """feed() 可被任意切分地调用；只有确定不会再形成敏感值的部分才输出。"""

    def __init__(self, secrets=None):
        self.secrets = sorted({str(value) for value in (secrets or []) if len(str(value)) >= MIN_SECRET},
                              key=len, reverse=True)
        self.window = max([len(value) for value in self.secrets] + [MIN_WINDOW])
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.buffer = ''
        self.count = 0

    def mask(self, text: str) -> str:
        for secret in self.secrets:
            if secret in text:
                text = text.replace(secret, MASK)
                self.count += 1
        for pattern in PATTERNS:
            text, found = pattern.subn(self._replace, text)
            self.count += found
        return text

    @staticmethod
    def _replace(match):
        if match.re.groups >= 3:
            return match.group(1) + match.group(2) + MASK
        if match.re.groups == 2:
            return match.group(1) + ': ' + MASK
        return MASK

    def feed(self, chunk: bytes) -> str:
        self.buffer += self.decoder.decode(chunk)
        if len(self.buffer) <= self.window:
            return ''
        boundary = len(self.buffer) - self.window
        while True:
            previous = boundary
            for secret in self.secrets:
                pos = self.buffer.find(secret, max(0, boundary - len(secret) + 1))
                while 0 <= pos < boundary:
                    if pos + len(secret) > boundary:
                        boundary = pos
                        break
                    pos = self.buffer.find(secret, pos + 1)
            if boundary == previous:
                break
        emit, self.buffer = self.buffer[:boundary], self.buffer[boundary:]
        return self.mask(emit)

    def flush(self) -> str:
        pending, self.buffer = self.buffer, ''
        return self.mask(pending + self.decoder.decode(b'', final=True))


def redact_chunks(chunks, secrets=None) -> str:
    """一次性工具，供受控测试与短输出使用。"""
    redactor = Redactor(secrets)
    return ''.join(redactor.feed(chunk) for chunk in chunks) + redactor.flush()
