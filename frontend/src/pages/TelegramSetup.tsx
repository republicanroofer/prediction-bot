import { useState } from "react";

const STEPS = [
  {
    title: "1. Create a Telegram Bot",
    content: [
      "Open Telegram and search for @BotFather",
      "Send /newbot and follow the prompts to name your bot",
      "Copy the HTTP API token (looks like 123456789:ABCdefGhIjKlMnOpQrStUvWxYz)",
    ],
  },
  {
    title: "2. Get Your Chat ID",
    content: [
      "Send any message to your new bot in Telegram",
      "Visit: https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates",
      "Find the chat.id field in the JSON response",
      "For group alerts, add the bot to a group and use the group chat ID (negative number)",
    ],
  },
  {
    title: "3. Configure the Bot",
    content: [
      "On the VPS, edit the .env file in the prediction-bot directory:",
      "  TELEGRAM_BOT_TOKEN=your_bot_token_here",
      "  TELEGRAM_CHAT_ID=your_chat_id_here",
      "  TELEGRAM_ERROR_CHAT_ID=your_error_chat_id (optional, defaults to main chat)",
      "Restart the bot: systemctl restart prediction-bot",
    ],
  },
  {
    title: "4. Verify",
    content: [
      "The bot sends alerts automatically for:",
      "  Position Opened — exchange, side, size, entry price, signal type",
      "  Position Closed — realized P&L, close reason",
      "  Whale Mirror — address, market, size, whale score",
      "  Drawdown Warning — when portfolio drawdown exceeds threshold",
      "  Budget Alert — when daily LLM spend nears the limit",
      "  Errors — component failures with details",
    ],
  },
];

export function TelegramSetup() {
  const [expanded, setExpanded] = useState<number>(0);

  return (
    <div className="space-y-4 max-w-2xl">
      <div className="flex items-center gap-3 mb-2">
        <h2 className="text-gray-300 text-sm font-semibold">Telegram Alerts Setup</h2>
        <span className="px-2 py-0.5 text-xs rounded-full bg-blue-900/50 text-blue-300 border border-blue-800">
          Guide
        </span>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <p className="text-gray-400 text-sm mb-4">
          Get real-time trade notifications, whale mirror alerts, and error warnings
          delivered directly to your Telegram chat.
        </p>

        <div className="space-y-2">
          {STEPS.map((step, i) => (
            <div key={i} className="border border-gray-800 rounded-lg overflow-hidden">
              <button
                onClick={() => setExpanded(expanded === i ? -1 : i)}
                className="w-full flex items-center justify-between px-4 py-3 text-left hover:bg-gray-800/50 transition-colors"
              >
                <span className="text-gray-200 text-sm font-medium">{step.title}</span>
                <span className="text-gray-500 text-xs">{expanded === i ? "−" : "+"}</span>
              </button>
              {expanded === i && (
                <div className="px-4 pb-3 space-y-1.5">
                  {step.content.map((line, j) => (
                    <p
                      key={j}
                      className={`text-sm ${line.startsWith("  ") ? "text-gray-500 font-mono text-xs pl-3" : "text-gray-400"}`}
                    >
                      {line}
                    </p>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
        <h3 className="text-gray-400 text-xs font-semibold uppercase mb-3">Alert Message Types</h3>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {[
            { emoji: "🟢", label: "Position Opened", desc: "Side, size, entry, confidence" },
            { emoji: "✅", label: "Position Closed", desc: "P&L, close reason" },
            { emoji: "🐋", label: "Whale Mirror", desc: "Address, market, score" },
            { emoji: "⚠️", label: "Drawdown Warning", desc: "Drawdown %, portfolio value" },
            { emoji: "💸", label: "Budget Alert", desc: "LLM spend approaching limit" },
            { emoji: "🚨", label: "Error Alert", desc: "Component failures" },
          ].map((a) => (
            <div key={a.label} className="flex items-start gap-2 p-2 rounded bg-gray-800/50">
              <span className="text-base shrink-0">{a.emoji}</span>
              <div>
                <div className="text-gray-300 text-xs font-semibold">{a.label}</div>
                <div className="text-gray-500 text-xs">{a.desc}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-yellow-900/20 border border-yellow-800/40 rounded-lg p-4">
        <p className="text-yellow-300 text-xs font-semibold uppercase mb-1">Rate Limits</p>
        <p className="text-yellow-200/70 text-sm">
          Telegram enforces ~30 messages/second per bot. The alerter throttles to 1 message/second
          per chat and automatically retries on 429 errors. For high-frequency setups, consider
          using a separate error chat ID to avoid flooding your main alert channel.
        </p>
      </div>
    </div>
  );
}
