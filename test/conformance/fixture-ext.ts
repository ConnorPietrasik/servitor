import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Conformance fixture extension: registers /svtest, which is CONSUMED by
// the prompt command (disposition "handled" — no run starts). Loaded only
// in the variant run via an extra -e; the production launch line does not
// include it (asserted in test_launch_line_live.py).
export default function (pi: ExtensionAPI) {
  pi.registerCommand("svtest", {
    description: "conformance: handled disposition probe",
    handler: async () => {
      // fire-and-forget: records, expects no response
      pi.sendMessage?.("user", "/svtest was handled by the fixture extension");
    },
  });
}
