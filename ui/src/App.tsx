import { Route, Routes } from "react-router-dom";
import Sessions from "./views/Sessions";
import Chat from "./views/Chat";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<Sessions />} />
      <Route path="/s/:sid" element={<Chat />} />
    </Routes>
  );
}
