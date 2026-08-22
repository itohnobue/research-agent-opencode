export const FixPrompt = async () => {
  const REMOVALS = [
    // --- Output suppression ---
    "IMPORTANT: You should minimize output tokens as much as possible while maintaining helpfulness, quality, and accuracy. Only address the specific query or task at hand, avoiding tangential information unless absolutely critical for completing the request. If you can answer in 1-3 sentences or a short paragraph, please do.",
    "IMPORTANT: You should NOT answer with unnecessary preamble or postamble (such as explaining your code or summarizing your action), unless the user asks you to.",
    "IMPORTANT: Keep your responses short, since they will be displayed on a command line interface. You MUST answer concisely with fewer than 4 lines (not including tool use or code generation), unless user asks for detail. Answer the user's question directly, without elaboration, explanation, or details. One word answers are best. Avoid introductions, conclusions, and explanations. You MUST avoid text before/after your response, such as \"The answer is <answer>.\", \"Here is the content of the file...\" or \"Based on the information provided, the answer is...\" or \"Here is what I will do next...\".",
    "You MUST answer concisely with fewer than 4 lines of text (not including tool use or code generation), unless user asks for detail.",

    // --- Prefix removals ---
    "You should be concise, direct, and to the point. ",

    // --- "Only use tools" contradicts action-reporting ---
    "Output text to communicate with the user; all text you output outside of tool use is displayed to the user. Only use tools to complete tasks. Never use tools like Bash or code comments as means to communicate with the user during the session.",

    // --- Tool / comments ---
    "- IMPORTANT: DO NOT ADD ***ANY*** COMMENTS unless asked",
    "- When doing file search, prefer to use the Task tool in order to reduce context usage.",

    // --- Anti-autonomy ---
    "You are allowed to be proactive, but only when the user asks you to do something. ",

    // --- "Implement yourself" contradicts delegation to agents ---
    "- Implement the solution using all tools available to you",

    // --- "Search yourself" contradicts delegation to research agents ---
    "- Use the available search tools to understand the codebase and the user's query. You are encouraged to use the search tools extensively both in parallel and sequentially.",

    // --- Anti-documentation ---
    "3. Do not add additional code explanation summary unless requested by the user. After working on a file, just stop, rather than providing an explanation of what you did.",

    // --- Length restriction on declining help ---
    ", and otherwise keep your response to 1-2 sentences.",

    // --- WebFetch mandate contradicts web_search.sh-only rule ---
    "When the user directly asks about opencode (eg 'can opencode do...', 'does opencode have...') or asks in second person (eg 'are you able...', 'can you do...'), first use the WebFetch tool to gather information to answer the question from opencode docs at https://opencode.ai",

    // --- Verbosity examples (one-word-answer teaching) ---
    " Here are some examples to demonstrate appropriate verbosity:\n<example>\nuser: what is 2+2?\nassistant: 4\n</example>\n\n<example>\nuser: is 11 a prime number?\nassistant: Yes\n</example>\n\n<example>\nuser: what command should I run to list files in the current directory?\nassistant: ls\n</example>\n\n<example>\nuser: what command should I run to watch files in the current directory?\nassistant: [use the ls tool to list the files in the current directory, then read docs/commands in the relevant file to find out how to watch files]\nnpm run dev\n</example>\n\n<example>\nuser: what files are in the directory src/?\nassistant: [runs ls and sees foo.c, bar.c, baz.c]\nuser: which file contains the implementation of foo?\nassistant: src/foo.c\n</example>\n\n<example>\nuser: write tests for new feature\nassistant: [uses grep and glob search tools to find where similar tests are defined, uses concurrent read file tool use blocks in one tool call to read relevant files at the same time, uses edit file tool to write new tests]\n</example>",
  ]

  return {
    "experimental.chat.system.transform": async (_input, output) => {
      try {
        if (!output?.system || !Array.isArray(output.system) || !output.system.length) return
        let text = output.system[0]
        if (typeof text !== "string") return
        if (!text.startsWith("You are opencode, an interactive CLI tool")) return

        let removed = 0
        for (const removal of REMOVALS) {
          const before = text.length
          text = text.replace(removal, "")
          if (text.length < before) removed++
        }
        text = text.replace(/\n{3,}/g, "\n\n")
        output.system[0] = text
      } catch (_) {}
    },
  }
}
