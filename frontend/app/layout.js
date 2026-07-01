import "./globals.css";

export const metadata = {
  title: "AI Loop v5",
  description: "EO-gated multi-agent system — chat interface",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body className="bg-neutral-950 text-neutral-100 min-h-screen">
        {children}
      </body>
    </html>
  );
}