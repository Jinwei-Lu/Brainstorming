"""JSON-RPC framing: what a real MCP client sees over stdio.

These run the server as its own process, because the stdin loop, the
"a notification produces no response" rule, and batch arrays are properties of
`main()` and `dispatch()`, not of any handler.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from harness import LiveServer, notification, request, rpc, server  # noqa: E402


class InitializeTest(unittest.TestCase):
    def test_initialize_echoes_a_supported_protocol_and_names_the_server(self) -> None:
        responses = rpc(
            [request(1, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})]
        )
        self.assertEqual(len(responses), 1)
        result = responses[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        self.assertIn(result["protocolVersion"], server.SUPPORTED_PROTOCOLS)
        self.assertEqual(result["serverInfo"]["name"], server.SERVER_NAME)
        self.assertEqual(result["serverInfo"]["version"], server.SERVER_VERSION)

    def test_initialize_falls_back_to_the_newest_protocol_when_asked_for_an_unknown_one(
        self,
    ) -> None:
        responses = rpc([request(1, "initialize", {"protocolVersion": "1999-01-01"})])
        self.assertEqual(responses[0]["result"]["protocolVersion"], server.SUPPORTED_PROTOCOLS[0])


class NotificationTest(unittest.TestCase):
    def test_a_notification_produces_no_stdout_line_at_all(self) -> None:
        """A JSON-RPC message without `id` must never be answered."""
        responses = rpc([notification("notifications/initialized")])
        self.assertEqual(responses, [])

    def test_the_line_after_a_notification_is_the_next_requests_answer(self) -> None:
        """Proven on a live process: the notification consumed no output line.

        Reading is never open-ended -- the very next line must be the `ping`
        response, so a stray notification reply would show up as a mismatch
        rather than as a hang.
        """
        live = LiveServer()
        self.addCleanup(live.close)
        live.send(request(1, "initialize", {"protocolVersion": "2025-11-25"}))
        self.assertEqual(live.read_json()["id"], 1)

        live.send(notification("notifications/initialized"))
        live.send(request(2, "ping"))
        self.assertEqual(live.read_json(), {"jsonrpc": "2.0", "id": 2, "result": {}})

        live.send(request(3, "ping"))
        self.assertEqual(live.read_json()["id"], 3, "the loop stays alive with stdin open")


class ToolsListTest(unittest.TestCase):
    def test_tools_list_advertises_exactly_the_sixteen_implemented_handlers(self) -> None:
        responses = rpc([request(1, "tools/list")])
        names = [tool["name"] for tool in responses[0]["result"]["tools"]]
        self.assertEqual(len(names), 16)
        self.assertNotIn(
            "idea_observation_register",
            names,
            "an observation is a question with a cost, not its own tool",
        )
        self.assertEqual(sorted(names), sorted(server.HANDLERS))
        self.assertEqual(len(set(names)), len(names), "tool names must be unique")


class UnknownToolTest(unittest.TestCase):
    def test_an_unknown_tool_is_refused_and_the_process_answers_the_next_request(self) -> None:
        """Refusing one call must not take the stdio loop down with it.

        The server answers an unknown tool with a JSON-RPC error object
        (-32602), not with an `isError` tool-result envelope; that envelope is
        reserved for a handler that ran and failed.
        """
        responses = rpc(
            [
                request(1, "tools/call", {"name": "idea_no_such_tool", "arguments": {}}),
                request(2, "ping"),
            ]
        )
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["id"], 1)
        self.assertEqual(responses[0]["error"]["code"], -32602)
        self.assertIn("idea_no_such_tool", responses[0]["error"]["message"])
        self.assertEqual(responses[1], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_a_handler_that_fails_returns_an_is_error_envelope_and_keeps_running(self) -> None:
        responses = rpc(
            [
                request(
                    1,
                    "tools/call",
                    {"name": "idea_tree_list_trees", "arguments": {"workspace": "not/absolute"}},
                ),
                request(2, "ping"),
            ]
        )
        self.assertEqual(len(responses), 2)
        result = responses[0]["result"]
        self.assertTrue(result["isError"])
        self.assertIn("absolute", result["structuredContent"]["error"]["message"])
        self.assertEqual(responses[1]["id"], 2)


class BatchTest(unittest.TestCase):
    def test_a_batch_of_two_requests_answers_both_in_order(self) -> None:
        batch = "[{}, {}]".format(request(7, "ping"), request(8, "tools/list"))
        responses = rpc([batch])
        self.assertEqual(len(responses), 1, "a batch must come back as one array line")
        array = responses[0]
        self.assertIsInstance(array, list)
        self.assertEqual([item["id"] for item in array], [7, 8])
        self.assertEqual(len(array[1]["result"]["tools"]), 16)

    def test_a_batch_of_only_notifications_produces_no_output(self) -> None:
        batch = "[{}, {}]".format(
            notification("notifications/initialized"),
            notification("notifications/cancelled", {"requestId": 1}),
        )
        self.assertEqual(rpc([batch]), [])


class MalformedInputTest(unittest.TestCase):
    def test_an_unparsable_line_returns_a_parse_error_and_the_loop_continues(self) -> None:
        responses = rpc(["{not json", request(3, "ping")])
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["id"], 3)


if __name__ == "__main__":
    unittest.main()
