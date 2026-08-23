"""Template contract for the bounded operations topology partial."""

import unittest
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

API_ROOT = Path(__file__).resolve().parents[1]
PARTIAL = "partials/system_topology.html"


class _MarkupProbe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.nodes: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))

    def handle_data(self, data):
        if data.strip():
            self.text.append(data.strip())

    def select(self, tag=None, class_name=None, **attrs):
        matches = []
        for node_tag, node_attrs in self.nodes:
            if tag is not None and node_tag != tag:
                continue
            if class_name is not None and class_name not in (
                node_attrs.get("class") or ""
            ).split():
                continue
            if any(node_attrs.get(name.replace("__", "-")) != value for name, value in attrs.items()):
                continue
            matches.append(node_attrs)
        return matches


def _topology(*, status="available", nodes=None, edges=None):
    return {
        "schema_version": 1,
        "generated_at": "2026-08-23T14:05:00+00:00",
        "status": status,
        "nodes": nodes
        if nodes is not None
        else [
            {
                "id": "event_bus",
                "label": "Event bus",
                "group": "Inputs",
                "kind": "event stream",
                "status": "active",
                "activity_state": "receiving",
                "bounded_count": 0,
                "last_activity_at": "2026-08-23T14:04:58+00:00",
                "staleness_reason": None,
                "safe_detail": "Validated events enter the research control plane.",
                "navigation_target": "/operations?component=events",
                "inferred_activity": False,
                "private_payload": {"secret": "must-not-render"},
            },
            {
                "id": "question_planner",
                "label": "Question planner",
                "group": "Research",
                "kind": "planner",
                "status": "healthy",
                "activity_state": "waiting",
                "bounded_count": 12,
                "last_activity_at": "2026-08-23T14:04:30+00:00",
                "staleness_reason": None,
                "safe_detail": "Plans bounded research questions from accepted events.",
                "navigation_target": None,
                "inferred_activity": True,
            },
            {
                "id": "thesis_store",
                "label": "Thesis store",
                "group": "Outputs",
                "kind": "read model",
                "status": "degraded",
                "activity_state": "idle",
                "bounded_count": None,
                "last_activity_at": None,
                "staleness_reason": "Latest verified effect is delayed.",
                "safe_detail": "Stores decision-support thesis effects.",
                "navigation_target": "/research",
                "inferred_activity": False,
            },
        ],
        "edges": edges
        if edges is not None
        else [
            {
                "source": "event_bus",
                "target": "question_planner",
                "kind": "event intake",
                "status": "active",
                "recent_activity_count": 0,
                "last_activity_at": "2026-08-23T14:04:58+00:00",
                "safe_detail": "Accepted event references only.",
            },
            {
                "source": "question_planner",
                "target": "thesis_store",
                "kind": "verified effect",
                "status": "degraded",
                "recent_activity_count": None,
                "last_activity_at": None,
                "safe_detail": "Verified material effects update decision support.",
            },
        ],
        "unavailable_components": ["forecast resolver"] if status == "partial" else [],
        "summary": "Events become bounded research questions and verified thesis effects.",
        "raw_debug": "must-not-render",
    }


class SystemTopologyPartialContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Environment(
            loader=FileSystemLoader(API_ROOT / "templates"),
            autoescape=select_autoescape(("html",)),
        )
        cls.template_source = (API_ROOT / "templates" / PARTIAL).read_text()

    def render(self, topology, *, live=True):
        return self.env.get_template(PARTIAL).render(
            topology=topology,
            live_updates_enabled=live,
        )

    def test_registers_one_canonical_live_section(self):
        rendered = self.render(_topology())
        probe = _MarkupProbe()
        probe.feed(rendered)

        registrations = probe.select(
            data__live__section="system_topology",
            data__live__event="system_topology_changed",
            data__live__url="/partials/operations/system-topology",
        )
        self.assertEqual(len(registrations), 1)
        self.assertNotIn("data-live-section", self.render(_topology(), live=False))

    def test_renders_bounded_inline_svg_layers_edges_and_nodes(self):
        topology = _topology()
        rendered = self.render(topology)
        probe = _MarkupProbe()
        probe.feed(rendered)

        svgs = probe.select("svg", class_name="system-topology-graph")
        self.assertEqual(len(svgs), 1)
        self.assertIn("viewbox", svgs[0])
        self.assertNotIn("width", svgs[0])
        self.assertNotIn("height", svgs[0])
        self.assertEqual(len(probe.select("g", class_name="system-topology-layer")), 3)
        self.assertEqual(len(probe.select("g", class_name="system-topology-node")), 3)
        self.assertEqual(len(probe.select("path", class_name="system-topology-edge-line")), 2)
        self.assertLessEqual(len(probe.select("g", class_name="system-topology-node")), 64)
        self.assertLessEqual(len(probe.select("path", class_name="system-topology-edge-line")), 128)

    def test_caps_direct_rendering_at_contract_bounds(self):
        prototype = _topology()["nodes"][0]
        nodes = [
            {
                **prototype,
                "id": f"node_{index}",
                "label": f"Node {index}",
                "group": f"Layer {index // 8}",
            }
            for index in range(70)
        ]
        edge = _topology()["edges"][0]
        edges = [
            {**edge, "source": "node_0", "target": "node_1"}
            for _ in range(140)
        ]
        probe = _MarkupProbe()
        probe.feed(self.render(_topology(nodes=nodes, edges=edges)))

        self.assertEqual(len(probe.select("g", class_name="system-topology-node")), 64)
        self.assertEqual(
            len(probe.select("path", class_name="system-topology-edge-line")), 128
        )

    def test_nodes_have_visible_status_focus_and_detail_associations(self):
        rendered = self.render(_topology(status="partial"))
        probe = _MarkupProbe()
        probe.feed(rendered)
        nodes = probe.select("g", class_name="system-topology-node")

        self.assertEqual({node.get("tabindex") for node in nodes}, {"0"})
        self.assertEqual({node.get("role") for node in nodes}, {"button"})
        for node in nodes:
            self.assertTrue(node.get("aria-label"))
            controls = node.get("aria-controls")
            self.assertTrue(controls and probe.select(id=controls))
        visible_text = " ".join(probe.text).lower()
        for value in ("active", "healthy", "degraded", "partial"):
            self.assertIn(value, visible_text)
        self.assertIn("forecast resolver", visible_text)
        self.assertIn("validated events enter the research control plane", visible_text)
        self.assertNotIn("must-not-render", rendered)

    def test_includes_legend_timestamp_and_readable_summary(self):
        rendered = self.render(_topology())
        probe = _MarkupProbe()
        probe.feed(rendered)

        self.assertEqual(len(probe.select("ul", class_name="system-topology-legend")), 1)
        self.assertEqual(
            probe.select("time", class_name="system-topology-updated")[0]["datetime"],
            "2026-08-23T14:05:00+00:00",
        )
        self.assertEqual(
            len(probe.select("section", class_name="system-topology-text-summary")),
            1,
        )
        visible_text = " ".join(probe.text)
        self.assertIn("Events become bounded research questions", visible_text)
        self.assertIn("Event bus to Question planner", visible_text)

    def test_has_mobile_containment_and_one_reduced_motion_hook(self):
        rendered = self.render(_topology())
        probe = _MarkupProbe()
        probe.feed(rendered)

        scrollers = probe.select(
            "div", class_name="system-topology-graph-scroll", data__topology__stack=""
        )
        self.assertEqual(len(scrollers), 1)
        self.assertNotIn('style="', rendered)
        self.assertGreaterEqual(len(probe.select(class_name="topology-activity")), 1)
        self.assertNotIn("animation", self.template_source.lower())

    def test_unavailable_and_no_data_states_are_distinct(self):
        unavailable = self.render(
            _topology(status="unavailable", nodes=[], edges=[])
        )
        empty = self.render(_topology(nodes=[], edges=[]))
        unavailable_probe = _MarkupProbe()
        unavailable_probe.feed(unavailable)

        self.assertIn("System topology is temporarily unavailable", unavailable)
        self.assertEqual(len(unavailable_probe.select(role="status")), 1)
        self.assertNotIn("<svg", unavailable)
        self.assertIn("No topology data has been recorded yet", empty)
        self.assertNotIn("<svg", empty)

    def test_has_no_external_graph_dependency_or_executable_script(self):
        source = self.template_source.lower()
        for prohibited in (
            "<script",
            "canvas",
            "cytoscape",
            "d3.js",
            "mermaid",
            "vis-network",
        ):
            self.assertNotIn(prohibited, source)
        self.assertIn("<svg", source)
        self.assertIn("<path", source)


if __name__ == "__main__":
    unittest.main()
