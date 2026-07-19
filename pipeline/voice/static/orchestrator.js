/**
 * Drives one full Estimator <-> Customer voice conversation, alternating turns
 * explicitly (see realtime-agent.js). Direct port of the alternating-turn loop
 * in estimator/scripts/run_estimator_eval_openai.py's run_conversation(), with
 * voice respond()/waitForDone() calls in place of text chat() calls.
 */
async function runTurnLoop(agentA, agentB, maxTurns, onTranscript) {
  let current = agentA;
  let other = agentB;
  current.respond();

  for (let i = 0; i < maxTurns * 2; i++) {
    const result = await current.waitForDone();
    onTranscript(current.label, result.transcript);
    if (result.toolCall) return result.toolCall;

    other.respond();
    [current, other] = [other, current];
  }
  throw new Error("Max turns reached without a tool call.");
}

async function runEstimatorCustomerDemo({ persona, maxTurns, onTranscript, onStatus }) {
  const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  const estimator = new RealtimeAgent("ESTIMATOR", audioCtx);
  const customer = new RealtimeAgent("CUSTOMER", audioCtx);

  try {
    onStatus("Connecting Estimator...");
    const estimatorStream = await estimator.connect("estimator");
    document.getElementById("estimator-audio").srcObject = estimatorStream;

    onStatus("Connecting Customer...");
    const customerStream = await customer.connect("customer", persona);
    document.getElementById("customer-audio").srcObject = customerStream;

    onStatus("Bridging audio between agents...");
    await wireTwoWay(estimator, customer);

    onStatus("Running the interview...");
    const toolCall = await runTurnLoop(estimator, customer, maxTurns, onTranscript);

    if (!toolCall || toolCall.name !== "submit_job_spec") {
      onStatus("Conversation ended without a submitted job spec.");
      return null;
    }

    onStatus("Validating the confirmed spec...");
    const validateRes = await fetch("/api/validate-spec", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(toolCall.arguments),
    });
    const validation = await validateRes.json();
    onStatus(validation.valid ? "Spec is valid." : "Spec has problems - see the JSON panel.");
    return { spec: toolCall.arguments, validation };
  } finally {
    estimator.close();
    customer.close();
  }
}
