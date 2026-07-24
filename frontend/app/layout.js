import "./globals.css";

export const metadata = {
  title: "MiniMe",
  description: "EO-gated multi-agent system — chat interface",
};

export default function RootLayout({ children }) {
  return (
    // suppressHydrationWarning: useDensity.js applies the saved
    // comfortable/compact preference to document.documentElement as
    // soon as the module is imported client-side (to avoid a flash of
    // the wrong density), which the server can never know about ahead
    // of time -- this is an intentional, expected mismatch on <html>.
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
