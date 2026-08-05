/**
 * TokenInput - Campo para ingresar múltiples valores separados por comas o Enter
 * Útil para auth_fields, oauth_scopes, allowed_agents, capabilities, etc.
 */

import React, { useState, KeyboardEvent } from 'react';
import { Input, Tag, TagGroup } from '@fluentui/react-components';
import { Dismiss12Regular } from '@fluentui/react-icons';

interface TokenInputProps {
  values: string[];
  onChange: (values: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
}

export const TokenInput: React.FC<TokenInputProps> = ({
  values,
  onChange,
  placeholder = 'Presiona Enter para agregar',
  disabled = false,
}) => {
  const [inputValue, setInputValue] = useState('');

  const addToken = (token: string) => {
    const trimmed = token.trim();
    if (trimmed && !values.includes(trimmed)) {
      onChange([...values, trimmed]);
    }
    setInputValue('');
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addToken(inputValue);
    } else if (e.key === ',' || e.key === ' ') {
      e.preventDefault();
      if (inputValue.trim()) {
        addToken(inputValue);
      }
    } else if (e.key === 'Backspace' && !inputValue && values.length > 0) {
      // Eliminar el último token si input está vacío
      onChange(values.slice(0, -1));
    }
  };

  const removeToken = (index: number) => {
    onChange(values.filter((_, i) => i !== index));
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {/* Tags actuales */}
      {values.length > 0 && (
        <TagGroup>
          {values.map((val, idx) => (
            <Tag
              key={idx}
              dismissible
              dismissIcon={{ children: <Dismiss12Regular /> }}
              onClick={() => !disabled && removeToken(idx)}
              size="small"
              appearance="outline"
            >
              {val}
            </Tag>
          ))}
        </TagGroup>
      )}

      {/* Input */}
      <Input
        value={inputValue}
        onChange={(_, d) => setInputValue(d.value)}
        onKeyDown={handleKeyDown}
        onBlur={() => {
          if (inputValue.trim()) addToken(inputValue);
        }}
        placeholder={placeholder}
        disabled={disabled}
        size="small"
      />
    </div>
  );
};
