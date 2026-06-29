export {};

declare global {
  interface Window {
    electronAPI?: {
      pickFile: (filters?: { name: string; extensions: string[] }[]) => Promise<string | null>;
      pickFolder: () => Promise<string | null>;
      getPathForFile: (file: File) => string | null;
    };
  }
}
