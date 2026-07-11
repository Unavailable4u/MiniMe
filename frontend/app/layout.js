import "./globals.css";

export const metadata = {
  title: "MiniMe",
  description: "EO-gated multi-agent system — chat interface",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
