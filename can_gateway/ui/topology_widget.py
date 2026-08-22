from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsPathItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QVBoxLayout,
    QWidget,
)


class PortNodeItem(QGraphicsRectItem):
    def __init__(self, name: str, x: float, y: float) -> None:
        super().__init__(x, y, 170, 56)
        self.name = name
        self.setPen(QPen(QColor("#2D4059"), 1.2))
        self.setBrush(QBrush(QColor("#EFF5FB")))
        label = QGraphicsSimpleTextItem(name, self)
        label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        box = self.rect()
        text_box = label.boundingRect()
        label.setPos(
            box.x() + (box.width() - text_box.width()) / 2,
            box.y() + (box.height() - text_box.height()) / 2,
        )

    def anchor_out(self, target_x: float) -> QPointF:
        rect = self.rect()
        if rect.x() < target_x:
            return QPointF(rect.x() + rect.width(), rect.y() + rect.height() / 2)
        return QPointF(rect.x(), rect.y() + rect.height() / 2)

    def anchor_in(self, source_x: float) -> QPointF:
        rect = self.rect()
        if rect.x() > source_x:
            return QPointF(rect.x(), rect.y() + rect.height() / 2)
        return QPointF(rect.x() + rect.width(), rect.y() + rect.height() / 2)

    def set_highlight(self, enabled: bool) -> None:
        if enabled:
            self.setPen(QPen(QColor("#D1495B"), 2.4))
            self.setBrush(QBrush(QColor("#FFF4F5")))
        else:
            self.setPen(QPen(QColor("#2D4059"), 1.2))
            self.setBrush(QBrush(QColor("#EFF5FB")))


class ClickView(QGraphicsView):
    clicked = Signal(object)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(event.pos())
        super().mousePressEvent(event)


@dataclass(slots=True)
class EdgeItem:
    source: str
    target: str
    path: QGraphicsPathItem
    arrow: QGraphicsPolygonItem


class TopologyPanel(QWidget):
    connection_requested = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.scene = QGraphicsScene(self)
        self.view = ClickView(self.scene)
        self.view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.view.setBackgroundBrush(QColor("#F8FAFC"))
        self.view.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.view.clicked.connect(self._on_view_clicked)

        layout = QVBoxLayout(self)
        layout.addWidget(self.view)

        self.nodes: Dict[str, PortNodeItem] = {}
        self.edges: List[EdgeItem] = []
        self.pending_source: str = ""

    def set_ports(self, port_names: List[str]) -> None:
        prev_connections = [(edge.source, edge.target) for edge in self.edges]
        self.scene.clear()
        self.nodes.clear()
        self.edges.clear()
        self.pending_source = ""

        names = sorted(port_names)
        left = names[::2]
        right = names[1::2]
        row_gap = 96
        for idx, name in enumerate(left):
            node = PortNodeItem(name, 30, 22 + idx * row_gap)
            self.scene.addItem(node)
            self.nodes[name] = node
        for idx, name in enumerate(right):
            node = PortNodeItem(name, 355, 22 + idx * row_gap)
            self.scene.addItem(node)
            self.nodes[name] = node

        height = max(len(left), len(right))
        self.scene.setSceneRect(0, 0, 600, max(220, (height + 1) * row_gap))
        self.set_connections(prev_connections)

    def set_connections(self, connections: List[Tuple[str, str]]) -> None:
        for edge in self.edges:
            self.scene.removeItem(edge.path)
            self.scene.removeItem(edge.arrow)
        self.edges.clear()

        seen: set[Tuple[str, str]] = set()
        for source, target in connections:
            key = (source, target)
            if key in seen:
                continue
            seen.add(key)
            if source in self.nodes and target in self.nodes and source != target:
                self._draw_edge(source, target)

    def _draw_edge(self, source: str, target: str) -> None:
        s = self.nodes[source]
        t = self.nodes[target]
        start = s.anchor_out(t.rect().x())
        end = t.anchor_in(s.rect().x())
        mid_x = (start.x() + end.x()) / 2

        path = QPainterPath(start)
        path.cubicTo(QPointF(mid_x, start.y()), QPointF(mid_x, end.y()), end)
        path_item = QGraphicsPathItem(path)
        path_item.setPen(QPen(QColor("#1F6FEB"), 2.0))
        self.scene.addItem(path_item)

        arrow = self._build_arrow(path, QColor("#1F6FEB"))
        self.scene.addItem(arrow)
        self.edges.append(EdgeItem(source=source, target=target, path=path_item, arrow=arrow))

    def _build_arrow(self, path: QPainterPath, color: QColor) -> QGraphicsPolygonItem:
        length = path.length() if hasattr(path, "length") else 1.0
        if length <= 0:
            length = 1.0
        tail = path.pointAtPercent(max(0.0, 1.0 - 12.0 / length))
        head = path.pointAtPercent(1.0)
        angle = math.atan2(head.y() - tail.y(), head.x() - tail.x())
        size = 8.0
        left = QPointF(
            head.x() - size * math.cos(angle - math.pi / 6),
            head.y() - size * math.sin(angle - math.pi / 6),
        )
        right = QPointF(
            head.x() - size * math.cos(angle + math.pi / 6),
            head.y() - size * math.sin(angle + math.pi / 6),
        )
        poly = QPolygonF([head, left, right])
        item = QGraphicsPolygonItem(poly)
        item.setBrush(QBrush(color))
        item.setPen(QPen(color))
        return item

    def _on_view_clicked(self, pos) -> None:
        scene_pos = self.view.mapToScene(pos)
        item = self.scene.itemAt(scene_pos, self.view.transform())
        if item is None:
            self._clear_pending()
            return
        node = item
        while node is not None and not isinstance(node, PortNodeItem):
            node = node.parentItem()
        if node is None:
            self._clear_pending()
            return
        self._handle_node_click(node.name)

    def _clear_pending(self) -> None:
        if self.pending_source and self.pending_source in self.nodes:
            self.nodes[self.pending_source].set_highlight(False)
        self.pending_source = ""

    def _handle_node_click(self, port_name: str) -> None:
        if not self.pending_source:
            self.pending_source = port_name
            self.nodes[port_name].set_highlight(True)
            return
        source = self.pending_source
        self.nodes[source].set_highlight(False)
        self.pending_source = ""
        if source == port_name:
            return
        self.connection_requested.emit(source, port_name)
