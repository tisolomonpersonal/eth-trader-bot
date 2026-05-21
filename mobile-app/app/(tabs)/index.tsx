import { WebViewScreen } from "@/components/webview-screen";
import { ScreenContainer } from "@/components/screen-container";

/**
 * Home Screen - ETH Trader Chat
 *
 * Displays the ETH Trader bot chat interface via WebView.
 * The WebView is wrapped to feel like a native mobile app.
 */
export default function HomeScreen() {
  return (
    <ScreenContainer edges={["top", "left", "right", "bottom"]} className="">
      <WebViewScreen url="https://tbot.zeabur.app/chat" />
    </ScreenContainer>
  );
}
