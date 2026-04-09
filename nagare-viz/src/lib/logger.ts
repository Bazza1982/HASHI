export type LogLevel = "info" | "warning" | "error";

export type DiagnosticLog = {
  id: string;
  timestamp: string;
  level: LogLevel;
  event: string;
  message: string;
};

export function createLog(level: LogLevel, event: string, message: string): DiagnosticLog {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    timestamp: new Date().toISOString(),
    level,
    event,
    message,
  };
}
