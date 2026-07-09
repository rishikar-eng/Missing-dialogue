import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { AuthProvider, useAuth } from "./auth";
import LoginScreen from "./screens/LoginScreen";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

// Auth gate: show the login screen until the user is signed in with their Rian account.
function Gate() {
  const { user } = useAuth();
  return user ? <App /> : <LoginScreen />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
