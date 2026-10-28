<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Заметки на Laravel</title>
    @vite(['resources/css/app.css', 'resources/js/app.js'])
</head>
<body class="mx-auto max-w-xl p-6 font-sans text-slate-800">
    <h1 class="mb-4 text-2xl font-semibold">Заметки</h1>
    <form method="post" action="/notes" class="mb-6 flex gap-2">
        @csrf
        <input name="text" maxlength="200" required class="flex-1 rounded border px-3 py-2" placeholder="Новая заметка">
        <button class="rounded bg-slate-800 px-4 py-2 text-white">Добавить</button>
    </form>
    @error('text')<p class="mb-4 text-red-600">{{ $message }}</p>@enderror
    <ul class="space-y-2">
        @forelse ($notes as $note)
            <li class="rounded border px-3 py-2">{{ $note->text }} <span class="text-sm text-slate-400">{{ $note->created_at->diffForHumans() }}</span></li>
        @empty
            <li class="text-slate-400">Пока пусто.</li>
        @endforelse
    </ul>
    <p class="mt-8 text-sm text-slate-400">Laravel {{ app()->version() }} · PHP {{ PHP_VERSION }}</p>
</body>
</html>
