import React from "react";

export function Greeting({ name }: { name: string }) {
  return <div>{`hello ${name}`}</div>;
}

export const Farewell = ({ name }: { name: string }) => <span>bye {name}</span>;
