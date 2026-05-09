import React from 'react';
import './App.css';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { HomePage, ChatPage } from './pages';
import { useWebSocket } from './hooks/useWebSocket';

function App() {
    useWebSocket();

  return (
    <Router>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="/home" element={<HomePage />} />
        {/* /plan/:planId renders the same ChatPage — plan is a state, not a separate page */}
        <Route path="/plan/:planId" element={<ChatPage />} />
        <Route path="/chat/:sessionId" element={<ChatPage />} />
        <Route path="/chat" element={<ChatPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Router>
  );
}

export default App;
