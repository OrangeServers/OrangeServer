// Continue a truncated text response without replaying tool calls.
export function installLengthRecovery(agent) {
  const previous = agent.finishTurn;
  const truncatedTexts = new Set();
  agent.finishTurn = async (turn, signal) => {
    const decision = await previous?.(turn, signal);
    if (decision || signal?.aborted) return decision;
    const text = turn.message?.content?.filter(x => x.type === 'text').map(x => x.text).join('') || '';
    if (turn.message?.stopReason === 'length' && text && !truncatedTexts.has(text) &&
        !turn.message.content?.some(x => x.type === 'toolCall')) {
      truncatedTexts.add(text);
      agent.followUp({ role: 'user', content: [{ type: 'text', text:
        '上一条回答被截断。请基于已完成的调查继续，给出结论和证据引用；不要重复执行已完成的操作。' }],
        timestamp: Date.now() });
    }
    return decision;
  };
}
