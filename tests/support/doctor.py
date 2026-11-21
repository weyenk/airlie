"""The doctor's fake probe and row lookup, shared by the tests that run its checks."""

from airlie.doctor import CheckResult, SystemProbe

TOOLS_OK = {
    "task": "3.54.0",
    "uv": "0.6.0",
    "git": "2.45.0",
    "node": "v26.10.0",
    "tart": "2.32.1",
    "softnet": "present (/opt/homebrew/bin/softnet)",
}


class FakeProbe(SystemProbe):
    def __init__(
        self,
        *,
        wired_mb: int | None = 106496,
        ram_gb: float = 128.0,
        tools: dict[str, str | None] | None = None,
        tart_images: list[str] | None = None,
    ) -> None:
        self._tart_images = ["ghcr.io/cirruslabs/ubuntu:latest"] if tart_images is None else tart_images
        self._wired = wired_mb
        self._ram = ram_gb
        self._tools = tools if tools is not None else dict(TOOLS_OK)

    def sysctl_int(self, name: str) -> int | None:
        return self._wired if name == "iogpu.wired_limit_mb" else None

    def total_ram_bytes(self) -> int:
        return int(self._ram * 2**30)

    def tart_images(self) -> list[str] | None:
        return self._tart_images

    def tart_vms(self):
        return getattr(self, "vms", None)

    def tool_version(self, name: str) -> str | None:
        return self._tools.get(name)

    def http_ok(self, url: str) -> bool:
        return True


def by_name(results: list[CheckResult]) -> dict[str, CheckResult]:
    return {r.name: r for r in results}
