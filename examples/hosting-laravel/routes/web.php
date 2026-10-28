<?php

use App\Models\Note;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;

Route::get('/', fn () => view('notes', ['notes' => Note::latest()->limit(20)->get()]));

Route::post('/notes', function (Request $request) {
    Note::create($request->validate(['text' => 'required|string|max:200']));

    return redirect('/');
});
