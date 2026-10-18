// The official `openai` client, pointed at the llm-router gateway: one base_url line.
// Streams a chat completion with one tool call. Run through main.py, which serves the gateway on
// the offline fake backend and passes its URL in OPENAI_BASE_URL.
import OpenAI from "openai";

const client = new OpenAI({ baseURL: process.env.OPENAI_BASE_URL, apiKey: "unused", maxRetries: 0 });
const tools: OpenAI.Chat.ChatCompletionTool[] = [{
  type: "function",
  function: {
    name: "get_weather", description: "Get the weather",
    parameters: { type: "object", properties: { city: { type: "string" } } },
  },
}];
const messages: OpenAI.Chat.ChatCompletionMessageParam[] = [
  { role: "user", content: "what is the weather in Lisbon right now" },
];

function assert(ok: boolean, message: string): void {
  if (!ok) throw new Error(message);
}

// Turn 1: the model asks for the tool (streamed tool-call deltas).
let name = "", args = "", id = "";
for await (const chunk of await client.chat.completions.create({ model: "gpt-4o", messages, tools, stream: true })) {
  for (const call of chunk.choices[0]?.delta?.tool_calls ?? []) {
    id ||= call.id ?? "";
    name += call.function?.name ?? "";
    args += call.function?.arguments ?? "";
  }
}
assert(name === "get_weather", `tool name: ${name}`);
assert(JSON.parse(args).city === "Lisbon", `tool arguments: ${args}`);

// Turn 2: send the tool result, stream the answer.
messages.push({ role: "assistant", content: null, tool_calls: [{ id, type: "function", function: { name, arguments: args } }] });
messages.push({ role: "tool", tool_call_id: id, content: "RESULT-SUNNY-21C" });
const pieces: string[] = [];
for await (const chunk of await client.chat.completions.create({ model: "gpt-4o", messages, tools, stream: true })) {
  const text = chunk.choices[0]?.delta?.content;
  if (text) pieces.push(text);
}
const answer = pieces.join("");
assert(answer === "It is sunny and 21C in Lisbon.", `answer: ${answer}`);
assert(pieces.length > 1, `the answer was not streamed (${pieces.length} piece)`);
console.log(answer);
