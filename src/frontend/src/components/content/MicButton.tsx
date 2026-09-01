import { Button } from '@fluentui/react-components';
import { Mic, PersonVoice } from '@/coral/imports/bundleicons';
import React from 'react';
import { useVoiceLive } from '../../hooks/useVoiceLive';
import { useDictation } from '../../hooks/useDictation';

interface MicButtonProps {
  /** dictation: voz→texto al input (STT). voicelive: gateway de voz alrededor del router. */
  mode?: 'dictation' | 'voicelive';
  disabled?: boolean;
  /** Only used in dictation mode: called with the final transcript text. */
  onTranscript?: (text: string) => void;
  /** Only used in voicelive mode: user speech transcript → send through the normal chat flow. */
  onUserTranscript?: (text: string) => void;
}

/** Dictation shell — uses useDictation internally */
const DictationButton: React.FC<{
  disabled?: boolean;
  onTranscript: (t: string) => void;
}> = ({ disabled, onTranscript }) => {
  const { recording, toggle } = useDictation(onTranscript);
  return (
    <Button
      appearance="subtle"
      icon={<Mic />}
      aria-label={recording ? 'Stop dictation' : 'Start dictation'}
      aria-pressed={recording}
      onClick={toggle}
      disabled={disabled}
      style={{
        height: '32px',
        width: '32px',
        borderRadius: '6px',
        backgroundColor: recording
          ? 'var(--colorBrandBackground)'
          : 'transparent',
        color: recording ? 'var(--colorNeutralBackgroundStatic)' : undefined,
      }}
    />
  );
};

/** VoiceLive shell — uses useVoiceLive internally */
const VoiceLiveButton: React.FC<{
  disabled?: boolean;
  onUserTranscript?: (t: string) => void;
}> = ({ disabled, onUserTranscript }) => {
  const { recording, toggle } = useVoiceLive(onUserTranscript);
  return (
    <Button
      appearance="subtle"
      icon={<PersonVoice />}
      aria-label={
        recording ? 'End voice conversation' : 'Start voice conversation'
      }
      aria-pressed={recording}
      onClick={toggle}
      disabled={disabled}
      style={{
        height: '32px',
        width: '32px',
        borderRadius: '6px',
        backgroundColor: recording
          ? 'var(--colorBrandBackground)'
          : 'transparent',
        color: recording ? 'var(--colorNeutralBackgroundStatic)' : undefined,
      }}
    />
  );
};

const MicButton: React.FC<MicButtonProps> = ({
  mode = 'dictation',
  disabled,
  onTranscript,
  onUserTranscript,
}) => {
  if (mode === 'voicelive')
    return (
      <VoiceLiveButton
        disabled={disabled}
        onUserTranscript={onUserTranscript}
      />
    );
  return (
    <DictationButton
      disabled={disabled}
      onTranscript={onTranscript ?? (() => {})}
    />
  );
};

export default MicButton;
