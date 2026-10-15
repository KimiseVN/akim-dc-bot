// Rounded chat bubble with a downward tail. Sized to the text inside;
// the parent positions it above the pet sprite.
export default function SpeechBubble({ text }: { text: string }): JSX.Element {
  return (
    <div className="bubble">
      <span>{text}</span>
      <div className="bubble-tail" />
    </div>
  );
}
