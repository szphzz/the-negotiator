/**
 * Cross-wires two RealtimeAgents so each one "hears" the other's spoken audio,
 * using one shared AudioContext (per agent A/B) to avoid cross-context
 * resampling issues. No renegotiation needed - replaceTrack() swaps each
 * agent's outgoing sender track live.
 */
async function wireTwoWay(agentA, agentB) {
  const [streamA, streamB] = await Promise.all([agentA.remoteStreamPromise, agentB.remoteStreamPromise]);
  const ctx = agentA.audioCtx;

  const sourceA = ctx.createMediaStreamSource(streamA);
  const destForB = ctx.createMediaStreamDestination();
  sourceA.connect(destForB);
  agentB.feedInputTrack(destForB.stream.getAudioTracks()[0]);

  const sourceB = ctx.createMediaStreamSource(streamB);
  const destForA = ctx.createMediaStreamDestination();
  sourceB.connect(destForA);
  agentA.feedInputTrack(destForA.stream.getAudioTracks()[0]);
}
