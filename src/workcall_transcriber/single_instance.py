"""Per-user Qt local-server lock for the tray application."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket


class SingleInstance:
    """Allow one GUI controller/watcher and activate it from later launches."""

    def __init__(self, name: str = "WorkCallTranscriber.LocalInstance") -> None:
        self._name = name
        self._server: QLocalServer | None = None
        self._on_activate: Callable[[], None] | None = None

    def acquire(self, on_activate: Callable[[], None]) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self._name)
        if socket.waitForConnected(250):
            socket.write(b"activate")
            socket.waitForBytesWritten(250)
            socket.disconnectFromServer()
            return False

        # No connected listener means a prior unclean exit may have left a stale
        # local-server endpoint. Remove only after the connection attempt failed.
        QLocalServer.removeServer(self._name)
        server = QLocalServer()
        if not server.listen(self._name):
            return False
        self._on_activate = on_activate
        self._server = server
        server.newConnection.connect(self._consume_activation)
        return True

    def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        QLocalServer.removeServer(self._name)
        self._server = None

    def _consume_activation(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readAll()
            socket.disconnectFromServer()
            if self._on_activate is not None:
                self._on_activate()
