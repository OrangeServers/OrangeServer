// Recheck server-owned evidence access immediately before every model call.
export function installAuthorizationGuard(agent, authorize) {
  const original = agent.streamFunction;
  agent.streamFunction = async (...args) => {
    if (!await authorize()) throw new Error('monitoring authorization unavailable');
    return original(...args);
  };
}
