/**
 * One side of a voice conversation over OpenAI's Realtime API (WebRTC).
 *
 * Turn-taking is driven explicitly (see respond()/waitForDone()) rather than
 * relying on server-side voice-activity-detection, which is unreliable against
 * synthetic AI speech instead of a real human mic - turn_detection is disabled
 * right after connecting for that reason.
 */
class RealtimeAgent {
  constructor(label, audioCtx) {
    this.label = label;
    this.audioCtx = audioCtx;
    this.pc = null;
    this.dc = null;
    this.sender = null;
    this.remoteStreamPromise = null;
    this._pendingDone = null;
    this._transcriptBuffer = "";
    this._toolCall = null;
    this.onTranscriptChunk = null; // optional (chunk: string) => void, for live UI updates
  }

  async connect(role, persona) {
    const res = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, persona }),
    });
    const data = await res.json();
    if (data.error) throw new Error(`${this.label}: ${data.error}`);
    const clientSecret = data.client_secret;

    const silentDest = this.audioCtx.createMediaStreamDestination();
    const silentTrack = silentDest.stream.getAudioTracks()[0];

    this.pc = new RTCPeerConnection();
    this.sender = this.pc.addTrack(silentTrack, silentDest.stream);

    this.remoteStreamPromise = new Promise((resolve) => {
      this.pc.ontrack = (e) => resolve(e.streams[0]);
    });

    this.dc = this.pc.createDataChannel("oai-events");
    this.dc.addEventListener("message", (e) => this._handleEvent(JSON.parse(e.data)));
    const dcOpen = new Promise((resolve) => this.dc.addEventListener("open", resolve, { once: true }));

    const offer = await this.pc.createOffer();
    await this.pc.setLocalDescription(offer);

    const sdpRes = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${clientSecret}`,
        "Content-Type": "application/sdp",
      },
      body: offer.sdp,
    });
    if (!sdpRes.ok) {
      throw new Error(`${this.label}: Realtime call setup failed (${sdpRes.status})`);
    }
    const answerSdp = await sdpRes.text();
    await this.pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

    await dcOpen;
    this._send({
      type: "session.update",
      session: { type: "realtime", audio: { input: { turn_detection: null } } },
    });

    return await this.remoteStreamPromise;
  }

  /** Swap in the audio this agent should "hear" (the other agent's voice). */
  feedInputTrack(track) {
    this.sender.replaceTrack(track);
  }

  /** Ask the model to speak its next turn now. */
  respond() {
    this._transcriptBuffer = "";
    this._toolCall = null;
    this._send({ type: "response.create" });
  }

  /** Resolves with {transcript, toolCall} once the in-flight response finishes. */
  waitForDone() {
    return new Promise((resolve) => {
      this._pendingDone = resolve;
    });
  }

  close() {
    if (this.dc) this.dc.close();
    if (this.pc) this.pc.close();
  }

  _send(obj) {
    this.dc.send(JSON.stringify(obj));
  }

  _handleEvent(evt) {
    if (evt.type === "response.audio_transcript.delta") {
      this._transcriptBuffer += evt.delta || "";
      if (this.onTranscriptChunk) this.onTranscriptChunk(this._transcriptBuffer);
    } else if (evt.type === "response.done") {
      const output = (evt.response && evt.response.output) || [];
      const fnCall = output.find((item) => item.type === "function_call");
      if (fnCall) {
        let args = {};
        try {
          args = JSON.parse(fnCall.arguments);
        } catch (e) {
          console.error(`${this.label}: failed to parse tool call arguments`, e);
        }
        this._toolCall = { name: fnCall.name, arguments: args };
      }
      const result = { transcript: this._transcriptBuffer, toolCall: this._toolCall };
      if (this._pendingDone) {
        const resolve = this._pendingDone;
        this._pendingDone = null;
        resolve(result);
      }
    }
  }
}
